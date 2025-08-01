# Copyright 2025 Flower Labs GmbH. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""Contextmanager for a gRPC request-response channel to the Flower server."""


from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from logging import ERROR
from pathlib import Path
from typing import Callable, Optional, Union, cast

import grpc
from cryptography.hazmat.primitives.asymmetric import ec

from flwr.common import GRPC_MAX_MESSAGE_LENGTH
from flwr.common.constant import HEARTBEAT_CALL_TIMEOUT, HEARTBEAT_DEFAULT_INTERVAL
from flwr.common.grpc import create_channel, on_channel_state_change
from flwr.common.heartbeat import HeartbeatSender
from flwr.common.inflatable_protobuf_utils import (
    make_confirm_message_received_fn_protobuf,
    make_pull_object_fn_protobuf,
    make_push_object_fn_protobuf,
)
from flwr.common.logger import log
from flwr.common.message import Message, remove_content_from_message
from flwr.common.retry_invoker import RetryInvoker, _wrap_stub
from flwr.common.secure_aggregation.crypto.symmetric_encryption import (
    generate_key_pairs,
)
from flwr.common.serde import message_from_proto, message_to_proto, run_from_proto
from flwr.common.typing import Fab, Run
from flwr.proto.fab_pb2 import GetFabRequest, GetFabResponse  # pylint: disable=E0611
from flwr.proto.fleet_pb2 import (  # pylint: disable=E0611
    CreateNodeRequest,
    DeleteNodeRequest,
    PullMessagesRequest,
    PullMessagesResponse,
    PushMessagesRequest,
    PushMessagesResponse,
)
from flwr.proto.fleet_pb2_grpc import FleetStub  # pylint: disable=E0611
from flwr.proto.heartbeat_pb2 import (  # pylint: disable=E0611
    SendNodeHeartbeatRequest,
    SendNodeHeartbeatResponse,
)
from flwr.proto.message_pb2 import ObjectTree  # pylint: disable=E0611
from flwr.proto.node_pb2 import Node  # pylint: disable=E0611
from flwr.proto.run_pb2 import GetRunRequest, GetRunResponse  # pylint: disable=E0611

from .client_interceptor import AuthenticateClientInterceptor
from .grpc_adapter import GrpcAdapter


@contextmanager
def grpc_request_response(  # pylint: disable=R0913,R0914,R0915,R0917
    server_address: str,
    insecure: bool,
    retry_invoker: RetryInvoker,
    max_message_length: int = GRPC_MAX_MESSAGE_LENGTH,  # pylint: disable=W0613
    root_certificates: Optional[Union[bytes, str]] = None,
    authentication_keys: Optional[
        tuple[ec.EllipticCurvePrivateKey, ec.EllipticCurvePublicKey]
    ] = None,
    adapter_cls: Optional[Union[type[FleetStub], type[GrpcAdapter]]] = None,
) -> Iterator[
    tuple[
        Callable[[], Optional[tuple[Message, ObjectTree]]],
        Callable[[Message, ObjectTree], set[str]],
        Callable[[], Optional[int]],
        Callable[[], None],
        Callable[[int], Run],
        Callable[[str, int], Fab],
        Callable[[int, str], bytes],
        Callable[[int, str, bytes], None],
        Callable[[int, str], None],
    ]
]:
    """Primitives for request/response-based interaction with a server.

    One notable difference to the grpc_connection context manager is that
    `receive` can return `None`.

    Parameters
    ----------
    server_address : str
        The IPv6 address of the server with `http://` or `https://`.
        If the Flower server runs on the same machine
        on port 8080, then `server_address` would be `"http://[::]:8080"`.
    insecure : bool
        Starts an insecure gRPC connection when True. Enables HTTPS connection
        when False, using system certificates if `root_certificates` is None.
    retry_invoker: RetryInvoker
        `RetryInvoker` object that will try to reconnect the client to the server
        after gRPC errors. If None, the client will only try to
        reconnect once after a failure.
    max_message_length : int
        Ignored, only present to preserve API-compatibility.
    root_certificates : Optional[Union[bytes, str]] (default: None)
        Path of the root certificate. If provided, a secure
        connection using the certificates will be established to an SSL-enabled
        Flower server. Bytes won't work for the REST API.
    authentication_keys : Optional[Tuple[PrivateKey, PublicKey]] (default: None)
        Tuple containing the elliptic curve private key and public key for
        authentication from the cryptography library.
        Source: https://cryptography.io/en/latest/hazmat/primitives/asymmetric/ec/
        Used to establish an authenticated connection with the server.
    adapter_cls: Optional[Union[type[FleetStub], type[GrpcAdapter]]] (default: None)
        A GrpcStub Class that can be used to send messages. By default the FleetStub
        will be used.

    Returns
    -------
    receive : Callable
    send : Callable
    create_node : Optional[Callable]
    delete_node : Optional[Callable]
    get_run : Optional[Callable]
    pull_object : Callable[[str], bytes]
    push_object : Callable[[str, bytes], None]
    confirm_message_received : Callable[[str], None]
    """
    if isinstance(root_certificates, str):
        root_certificates = Path(root_certificates).read_bytes()

    # Automatic node auth: generate keys if user didn't provide any
    if authentication_keys is None:
        authentication_keys = generate_key_pairs()

    # Always configure auth interceptor, with either user-provided or generated keys
    interceptors: Sequence[grpc.UnaryUnaryClientInterceptor] = [
        AuthenticateClientInterceptor(*authentication_keys),
    ]
    channel = create_channel(
        server_address=server_address,
        insecure=insecure,
        root_certificates=root_certificates,
        max_message_length=max_message_length,
        interceptors=interceptors,
    )
    channel.subscribe(on_channel_state_change)

    # Shared variables for inner functions
    if adapter_cls is None:
        adapter_cls = FleetStub
    stub = adapter_cls(channel)
    node: Optional[Node] = None

    # Wrap stub
    _wrap_stub(stub, retry_invoker)
    ###########################################################################
    # send_node_heartbeat/create_node/delete_node/receive/send/get_run functions
    ###########################################################################

    def send_node_heartbeat() -> bool:
        # Get Node
        if node is None:
            log(ERROR, "Node instance missing")
            return False

        # Construct the heartbeat request
        req = SendNodeHeartbeatRequest(
            node=node, heartbeat_interval=HEARTBEAT_DEFAULT_INTERVAL
        )

        # Call FleetAPI
        try:
            res: SendNodeHeartbeatResponse = stub.SendNodeHeartbeat(
                req, timeout=HEARTBEAT_CALL_TIMEOUT
            )
        except grpc.RpcError as e:
            status_code = e.code()
            if status_code == grpc.StatusCode.UNAVAILABLE:
                return False
            if status_code == grpc.StatusCode.DEADLINE_EXCEEDED:
                return False
            raise

        # Check if success
        if not res.success:
            raise RuntimeError(
                "Heartbeat failed unexpectedly. The SuperLink does not "
                "recognize this SuperNode."
            )
        return True

    heartbeat_sender = HeartbeatSender(send_node_heartbeat)

    def create_node() -> Optional[int]:
        """Set create_node."""
        # Call FleetAPI
        create_node_request = CreateNodeRequest(
            heartbeat_interval=HEARTBEAT_DEFAULT_INTERVAL
        )
        create_node_response = stub.CreateNode(request=create_node_request)

        # Remember the node and start the heartbeat sender
        nonlocal node
        node = cast(Node, create_node_response.node)
        heartbeat_sender.start()
        return node.node_id

    def delete_node() -> None:
        """Set delete_node."""
        # Get Node
        nonlocal node
        if node is None:
            log(ERROR, "Node instance missing")
            return

        # Stop the heartbeat sender
        heartbeat_sender.stop()

        # Call FleetAPI
        delete_node_request = DeleteNodeRequest(node=node)
        stub.DeleteNode(request=delete_node_request)

        # Cleanup
        node = None

    def receive() -> Optional[tuple[Message, ObjectTree]]:
        """Pull a message with its ObjectTree from SuperLink."""
        # Get Node
        if node is None:
            log(ERROR, "Node instance missing")
            return None

        # Try to pull a message with its object tree from SuperLink
        request = PullMessagesRequest(node=node)
        response: PullMessagesResponse = stub.PullMessages(request=request)

        # If no messages are available, return None
        if len(response.messages_list) == 0:
            return None

        # Get the current Message and its object tree
        message_proto = response.messages_list[0]
        object_tree = response.message_object_trees[0]

        # Construct the Message
        in_message = message_from_proto(message_proto)

        # Return the Message and its object tree
        return in_message, object_tree

    def send(message: Message, object_tree: ObjectTree) -> set[str]:
        """Send the message with its ObjectTree to SuperLink."""
        # Get Node
        if node is None:
            log(ERROR, "Node instance missing")
            return set()

        # Remove the content from the message if it has
        if message.has_content():
            message = remove_content_from_message(message)

        # Send the message with its ObjectTree to SuperLink
        request = PushMessagesRequest(
            node=node,
            messages_list=[message_to_proto(message)],
            message_object_trees=[object_tree],
        )
        response: PushMessagesResponse = stub.PushMessages(request=request)

        # Get and return the object IDs to push
        return set(response.objects_to_push)

    def get_run(run_id: int) -> Run:
        # Call FleetAPI
        get_run_request = GetRunRequest(node=node, run_id=run_id)
        get_run_response: GetRunResponse = stub.GetRun(request=get_run_request)

        # Return fab_id and fab_version
        return run_from_proto(get_run_response.run)

    def get_fab(fab_hash: str, run_id: int) -> Fab:
        # Call FleetAPI
        get_fab_request = GetFabRequest(node=node, hash_str=fab_hash, run_id=run_id)
        get_fab_response: GetFabResponse = stub.GetFab(request=get_fab_request)

        return Fab(get_fab_response.fab.hash_str, get_fab_response.fab.content)

    def pull_object(run_id: int, object_id: str) -> bytes:
        """Pull the object from the SuperLink."""
        # Check Node
        if node is None:
            raise RuntimeError("Node instance missing")

        fn = make_pull_object_fn_protobuf(
            pull_object_protobuf=stub.PullObject,
            node=node,
            run_id=run_id,
        )
        return fn(object_id)

    def push_object(run_id: int, object_id: str, contents: bytes) -> None:
        """Push the object to the SuperLink."""
        # Check Node
        if node is None:
            raise RuntimeError("Node instance missing")

        fn = make_push_object_fn_protobuf(
            push_object_protobuf=stub.PushObject,
            node=node,
            run_id=run_id,
        )
        fn(object_id, contents)

    def confirm_message_received(run_id: int, object_id: str) -> None:
        """Confirm that the message has been received."""
        # Check Node
        if node is None:
            raise RuntimeError("Node instance missing")

        fn = make_confirm_message_received_fn_protobuf(
            confirm_message_received_protobuf=stub.ConfirmMessageReceived,
            node=node,
            run_id=run_id,
        )
        fn(object_id)

    try:
        # Yield methods
        yield (
            receive,
            send,
            create_node,
            delete_node,
            get_run,
            get_fab,
            pull_object,
            push_object,
            confirm_message_received,
        )
    except Exception as exc:  # pylint: disable=broad-except
        log(ERROR, exc)
    # Cleanup
    finally:
        try:
            if node is not None:
                # Disable retrying
                retry_invoker.max_tries = 1
                delete_node()
        except grpc.RpcError:
            pass
        channel.close()
