import json
import uuid
import requests
import time
import sys
import locale
from typing import Dict, Any, Optional
from urllib.parse import urljoin

# 支持相对导入和绝对导入
try:
    from .sse_handler import SSEHandler
except ImportError:
    from sse_handler import SSEHandler

# 设置输出编码为UTF-8
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
if sys.stderr.encoding != "utf-8":
    sys.stderr.reconfigure(encoding="utf-8")


class Client:
    """
    MCP (Model Context Protocol) Client implementation.

    Example usage:
        # Create client with custom name, server host, and SSE path
        client = Client("MyApp", "http://localhost:3000", "/sse")

        # Or use default SSE path "/sse"
        client = Client("MyApp", "http://localhost:3000")

        # Initialize connection to MCP server via SSE
        if client.initialize():
            print(f"[{self.client_name}]: Connected to MCP server successfully!")
            # Use the client for MCP operations
            client.disconnect()  # Clean up when done
        else:
            print(f"[{self.client_name}]: Failed to connect to MCP server")
    """

    def __init__(
        self,
        client_name: str = "MCPClient",
        host: str = "http://localhost:3000",
        sse_path: str = "/sse",
    ):
        self.protocol_version = "2024-11-05"
        self.client_name = client_name
        self.client_version = "1.0.0"
        self.host = host.rstrip("/")  # Remove trailing slash if present
        self.sse_path = sse_path  # SSE endpoint path
        self.initialized = False
        self.server_capabilities = {}
        self.server_info = {}

        # SSE handler for managing SSE connections
        self.sse_handler = SSEHandler(self.host, self.sse_path, self.client_name)

        # Endpoint for sending messages (set by SSE handler callback)
        self.sse_endpoint = None
        self.message_queue = {}  # Store pending responses by request ID

    def initialize(self) -> bool:
        """
        Initialize MCP client using SSE transport according to MCP specification.

        Returns:
            bool: True if initialization was successful
        """
        try:
            # Register SSE event callbacks
            self.sse_handler.on_endpoint = self._on_sse_endpoint
            self.sse_handler.on_message = self._on_sse_message

            # Step 1: Establish SSE connection
            if not self.sse_handler.connect():
                return False

            # Step 2: Wait for endpoint event
            if not self.sse_handler.wait_for_endpoint():
                return False

            # Step 3: Send initialize request (response will be handled by SSE listener)
            if not self._send_initialize_request():
                return False

            # Wait for initialization to complete (handled by SSE listener)
            timeout = 10  # 10 seconds timeout
            start_time = time.time()
            while not self.initialized and (time.time() - start_time) < timeout:
                time.sleep(0.1)

            if not self.initialized:
                print(
                    f"[{self.client_name}]: Timeout waiting for initialization to complete"
                )
                return False

            return True

        except Exception as e:
            print(f"[{self.client_name}]: Unexpected error during initialization: {e}")
            return False

    def list_tools(self) -> bool:
        """
        Send tools request to the server.

        Returns:
            bool: True if request sent and response received successfully
        """
        # Generate unique request ID
        request_id = str(uuid.uuid4())

        # Create tools/list request according to MCP specification
        tool_list_request = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/list",
            "params": {
                "cursor": "0",
            },
        }

        try:
            # Send request to the endpoint provided by server
            # Note: tools/list response will be handled by _handle_message_event
            response = requests.post(
                self.sse_endpoint,
                json=tool_list_request,
                headers={"Content-Type": "application/json"},
                timeout=30,
            )
            response.raise_for_status()

            # print("Tools list request sent successfully")
            return True

        except requests.exceptions.RequestException as e:
            print(f"[{self.client_name}]: Failed to send tools/list request: {e}")
            return False

    def _on_sse_endpoint(self, endpoint_url: str):
        """
        Callback for SSE endpoint event.

        Args:
            endpoint_url: The endpoint URL from the server
        """
        self.sse_endpoint = endpoint_url
        print(f"[{self.client_name}]: Received endpoint: {endpoint_url}")

    def _on_sse_message(self, data: str):
        """
        Callback for SSE message event.

        Args:
            data: The message data from the SSE event
        """
        # Server sent a message response
        try:
            # 尝试解析JSON
            message = json.loads(data)
            request_id = message.get("id")

            # Check if this is an initialize response
            if self._is_initialize_response(message):
                self.handle_initialize(message)
            else:
                self.handle_normal_message(message)

            # Store in message queue for other requests
            if request_id in self.message_queue:
                self.message_queue[request_id] = message

        except json.JSONDecodeError as e:
            print(f"[{self.client_name}]: Failed to parse SSE message: {e}")
            print(f"[{self.client_name}]: Raw data length: {len(data)}")
            print(
                f"[{self.client_name}]: Raw data (first 200 chars): {repr(data[:200])}"
            )
            print(
                f"[{self.client_name}]: Raw data (last 200 chars): {repr(data[-200:])}"
            )

    def _is_initialize_response(self, message: Dict[str, Any]) -> bool:
        """
        Check if the message is an initialize response.
        """
        return (
            message.get("id")
            and hasattr(self, "_current_initialize_id")
            and message.get("id") == self._current_initialize_id
        )

    def handle_initialize(self, message: Dict[str, Any]) -> bool:
        """
        Handle the initialize response.
        """
        # Handle initialize response
        if self.handle_initialize_response(message):
            # Send initialized notification
            self._send_initialized_notification()
            self.initialized = True
            print(f"[{self.client_name}]: MCP client initialized successfully via SSE")
        else:
            print(f"[{self.client_name}]: Failed to handle initialize response")
        return True

    def handle_normal_message(self, message: Dict[str, Any]) -> bool:
        print(f"[{self.client_name}]: Received normal message: {message}")
        return True

    def _send_initialize_request(self) -> bool:
        """
        Send initialize request to the server.

        Returns:
            bool: True if request sent and response received successfully
        """
        # Generate unique request ID
        request_id = str(uuid.uuid4())

        # Client capabilities as per MCP specification
        client_capabilities = {"roots": {"listChanged": True}, "sampling": {}}

        # Client info
        client_info = {"name": self.client_name, "version": self.client_version}

        # Create initialize request according to MCP specification
        initialize_request = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "initialize",
            "params": {
                "protocolVersion": self.protocol_version,
                "capabilities": client_capabilities,
                "clientInfo": client_info,
            },
        }

        try:
            # Store the request ID for SSE listener to handle
            self._current_initialize_id = request_id

            # Send request to the endpoint provided by server
            response = requests.post(
                self.sse_endpoint,
                json=initialize_request,
                headers={"Content-Type": "application/json"},
                timeout=30,
            )
            response.raise_for_status()

            # print("Initialize request sent successfully")
            return True

        except requests.exceptions.RequestException as e:
            print(f"[{self.client_name}]: Failed to send initialize request: {e}")
            return False

    def _send_initialized_notification(self) -> bool:
        """
        Send initialized notification to the server.

        Returns:
            bool: True if notification sent successfully
        """
        initialized_notification = {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
        }

        try:
            response = requests.post(
                self.sse_endpoint,
                json=initialized_notification,
                headers={"Content-Type": "application/json"},
                timeout=30,
            )
            response.raise_for_status()
            return True

        except requests.exceptions.RequestException as e:
            print(f"[{self.client_name}]: Failed to send initialized notification: {e}")
            return False

    def handle_initialize_response(self, response: Dict[str, Any]) -> bool:
        """
        Handle the server's response to the initialize request.

        Args:
            response: Server's initialize response

        Returns:
            bool: True if initialization was successful
        """
        if "error" in response:
            print(f"[{self.client_name}]: Initialize error: {response['error']}")
            return False

        if "result" not in response:
            print(f"[{self.client_name}]: Invalid initialize response: missing result")
            return False

        result = response["result"]

        # Validate protocol version compatibility
        server_protocol_version = result.get("protocolVersion")
        if server_protocol_version != self.protocol_version:
            print(
                f"[{self.client_name}]: Protocol version mismatch: client={self.protocol_version}, server={server_protocol_version}"
            )
            return False

        # Store server capabilities and info
        self.server_capabilities = result.get("capabilities", {})
        self.server_info = result.get("serverInfo", {})

        print(f"[{self.client_name}]: Server capabilities: {self.server_capabilities}")
        print(f"[{self.client_name}]: Server info: {self.server_info}")

        return True

    def disconnect(self):
        """
        Disconnect from the MCP server and clean up resources.
        """
        try:
            if self.sse_handler:
                self.sse_handler.disconnect()
            self.initialized = False
            print(f"[{self.client_name}]: Disconnected from MCP server")
        except Exception as e:
            print(f"[{self.client_name}]: Error during disconnect: {e}")

    def send(self, message):
        pass


if __name__ == "__main__":
    # Example usage with SSE transport
    # You can customize the SSE path based on your server configuration
    client = Client(
        client_name="MyApp",
        host="http://127.0.0.1:8181",
        sse_path="/sse",  # Customize this path as needed
    )
    if client.initialize():
        # print("MCP client initialized successfully!")
        # Perform MCP operations here
        client.list_tools()
        client.disconnect()
    else:
        print(f"Failed to initialize MCP client")
