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
"""Flower Exec API interceptor."""


import contextvars
from typing import Any, Callable, Union

import grpc

from flwr.common.auth_plugin import ExecAuthPlugin, ExecAuthzPlugin
from flwr.common.typing import AccountInfo
from flwr.proto.exec_pb2 import (  # pylint: disable=E0611
    GetAuthTokensRequest,
    GetAuthTokensResponse,
    GetLoginDetailsRequest,
    GetLoginDetailsResponse,
    StartRunRequest,
    StartRunResponse,
    StreamLogsRequest,
    StreamLogsResponse,
)

Request = Union[
    StartRunRequest,
    StreamLogsRequest,
    GetLoginDetailsRequest,
    GetAuthTokensRequest,
]

Response = Union[
    StartRunResponse, StreamLogsResponse, GetLoginDetailsResponse, GetAuthTokensResponse
]


shared_account_info: contextvars.ContextVar[AccountInfo] = contextvars.ContextVar(
    "account_info", default=AccountInfo(flwr_aid=None, account_name=None)
)


class ExecUserAuthInterceptor(grpc.ServerInterceptor):  # type: ignore
    """Exec API interceptor for user authentication."""

    def __init__(
        self,
        auth_plugin: ExecAuthPlugin,
        authz_plugin: ExecAuthzPlugin,
    ):
        self.auth_plugin = auth_plugin
        self.authz_plugin = authz_plugin

    def intercept_service(
        self,
        continuation: Callable[[Any], Any],
        handler_call_details: grpc.HandlerCallDetails,
    ) -> grpc.RpcMethodHandler:
        """Flower server interceptor authentication logic.

        Intercept all unary-unary/unary-stream calls from users and authenticate users
        by validating auth metadata sent by the user. Continue RPC call if user is
        authenticated, else, terminate RPC call by setting context to abort.
        """
        # Only apply to Exec service
        if not handler_call_details.method.startswith("/flwr.proto.Exec/"):
            return continuation(handler_call_details)

        # One of the method handlers in
        # `flwr.superlink.servicer.exec.ExecServicer`
        method_handler: grpc.RpcMethodHandler = continuation(handler_call_details)
        return self._generic_auth_unary_method_handler(method_handler)

    def _generic_auth_unary_method_handler(
        self, method_handler: grpc.RpcMethodHandler
    ) -> grpc.RpcMethodHandler:
        def _generic_method_handler(
            request: Request,
            context: grpc.ServicerContext,
        ) -> Response:
            call = method_handler.unary_unary or method_handler.unary_stream
            metadata = context.invocation_metadata()

            # Intercept GetLoginDetails and GetAuthTokens requests, and return
            # the response without authentication
            if isinstance(request, (GetLoginDetailsRequest, GetAuthTokensRequest)):
                return call(request, context)  # type: ignore

            # For other requests, check if the user is authenticated
            valid_tokens, account_info = self.auth_plugin.validate_tokens_in_metadata(
                metadata
            )
            if valid_tokens:
                if account_info is None:
                    context.abort(
                        grpc.StatusCode.UNAUTHENTICATED,
                        "Tokens validated, but user info not found",
                    )
                    raise grpc.RpcError()
                # Store user info in contextvars for authenticated users
                shared_account_info.set(account_info)
                # Check if the user is authorized
                if not self.authz_plugin.verify_user_authorization(account_info):
                    context.abort(
                        grpc.StatusCode.PERMISSION_DENIED,
                        "❗️ User not authorized. "
                        "Please contact the SuperLink administrator.",
                    )
                    raise grpc.RpcError()
                return call(request, context)  # type: ignore

            # If the user is not authenticated, refresh tokens
            tokens, account_info = self.auth_plugin.refresh_tokens(metadata)
            if tokens is not None:
                if account_info is None:
                    context.abort(
                        grpc.StatusCode.UNAUTHENTICATED,
                        "Tokens refreshed, but user info not found",
                    )
                    raise grpc.RpcError()
                # Store user info in contextvars for authenticated users
                shared_account_info.set(account_info)
                # Check if the user is authorized
                if not self.authz_plugin.verify_user_authorization(account_info):
                    context.abort(
                        grpc.StatusCode.PERMISSION_DENIED,
                        "❗️ User not authorized. "
                        "Please contact the SuperLink administrator.",
                    )
                    raise grpc.RpcError()

                context.send_initial_metadata(tokens)
                return call(request, context)  # type: ignore

            context.abort(grpc.StatusCode.UNAUTHENTICATED, "Access denied")
            raise grpc.RpcError()  # This line is unreachable

        if method_handler.unary_unary:
            message_handler = grpc.unary_unary_rpc_method_handler
        else:
            message_handler = grpc.unary_stream_rpc_method_handler
        return message_handler(
            _generic_method_handler,
            request_deserializer=method_handler.request_deserializer,
            response_serializer=method_handler.response_serializer,
        )
