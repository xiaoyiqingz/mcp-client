import requests
import threading
import time
import socket
from typing import Optional, Callable


class SSEHandler:
    """
    SSE (Server-Sent Events) 连接处理器

    负责管理 SSE 连接、解析事件，并通过回调函数通知调用者
    """

    def __init__(self, host: str, sse_path: str, logger_name: str = "SSEHandler"):
        """
        初始化 SSE 处理器

        Args:
            host: 服务器地址
            sse_path: SSE 端点路径
            logger_name: 日志名称（用于打印日志）
        """
        self.host = host.rstrip("/")  # Remove trailing slash if present
        self.sse_path = sse_path
        self.logger_name = logger_name

        # 连接状态
        self.sse_connection: Optional[requests.Response] = None
        self.sse_thread: Optional[threading.Thread] = None
        self.connection_established = False
        self.sse_endpoint: Optional[str] = None  # 从服务器接收到的消息端点

        # 回调函数（由调用者注册）
        self.on_endpoint: Optional[Callable[[str], None]] = (
            None  # callback(endpoint_url: str)
        )
        self.on_message: Optional[Callable[[str], None]] = None  # callback(data: str)
        self.on_error: Optional[Callable[[Exception], None]] = (
            None  # callback(error: Exception)
        )
        self.on_disconnect: Optional[Callable[[], None]] = None  # callback()

    def connect(self, timeout: float = 10) -> bool:
        """
        建立 SSE 连接

        Args:
            timeout: 连接超时时间（秒）

        Returns:
            bool: True if connection established successfully
        """
        try:
            # Start SSE connection in a separate thread
            self.sse_thread = threading.Thread(target=self._sse_listener, daemon=True)
            self.sse_thread.start()

            # Wait for connection to be established
            start_time = time.time()
            while (
                not self.connection_established and (time.time() - start_time) < timeout
            ):
                time.sleep(0.1)

            if not self.connection_established:
                print(
                    f"[{self.logger_name}]: Failed to establish SSE connection within timeout"
                )
                return False

            print(f"[{self.logger_name}]: SSE connection established")
            return True

        except Exception as e:
            print(f"[{self.logger_name}]: Failed to establish SSE connection: {e}")
            if self.on_error:
                self.on_error(e)
            return False

    def disconnect(self):
        """
        断开 SSE 连接并清理资源
        """
        try:
            # 先标记连接为已断开
            self.connection_established = False

            # 关闭连接，强制中断 iter_lines() 的阻塞等待
            if self.sse_connection:
                try:
                    # 通过 urllib3 连接池关闭底层 socket，立即中断 iter_lines() 的阻塞
                    if hasattr(self.sse_connection, "raw") and self.sse_connection.raw:
                        # 获取底层连接
                        if hasattr(self.sse_connection.raw, "_connection"):
                            conn = self.sse_connection.raw._connection
                            if conn:
                                # 尝试获取 socket
                                sock = None
                                if hasattr(conn, "sock"):
                                    sock = conn.sock
                                elif hasattr(conn, "_sock"):
                                    sock = conn._sock

                                if sock and isinstance(sock, socket.socket):
                                    try:
                                        # 设置 socket 为非阻塞模式，这样读取会立即返回
                                        sock.settimeout(0.0)
                                        # 使用 shutdown 立即中断 socket 读取/写入
                                        # 这是关键：能立即中断 iter_lines() 的阻塞等待
                                        sock.shutdown(socket.SHUT_RDWR)
                                    except (OSError, socket.error):
                                        # Socket 可能已经关闭
                                        pass

                                # 关闭连接对象
                                try:
                                    conn.close()
                                except:
                                    pass

                        # 关闭 raw 连接
                        try:
                            self.sse_connection.raw.close()
                        except:
                            pass

                    # 关闭响应对象（额外的清理步骤）
                    try:
                        self.sse_connection.close()
                    except:
                        pass
                except Exception as close_error:
                    # 记录但不抛出异常
                    print(
                        f"[{self.logger_name}]: Error closing connection: {close_error}"
                    )
                finally:
                    self.sse_connection = None

            self.sse_endpoint = None
            if self.on_disconnect:
                self.on_disconnect()
            print(f"[{self.logger_name}]: SSE connection disconnected")
        except Exception as e:
            print(f"[{self.logger_name}]: Error during disconnect: {e}")

    def wait_for_endpoint(self, timeout: float = 10) -> bool:
        """
        等待服务器发送 endpoint 事件

        Args:
            timeout: 等待超时时间（秒）

        Returns:
            bool: True if endpoint received successfully
        """
        start_time = time.time()

        while self.sse_endpoint is None and (time.time() - start_time) < timeout:
            time.sleep(0.1)

        if self.sse_endpoint is None:
            print(f"[{self.logger_name}]: Timeout waiting for endpoint from server")
            return False

        return True

    def _sse_listener(self):
        """
        监听 SSE 事件的主循环
        """
        try:
            response = requests.get(
                f"{self.host}{self.sse_path}",
                headers={"Accept": "text/event-stream"},
                stream=True,
                timeout=None,
            )
            response.raise_for_status()

            # 明确设置编码为UTF-8，避免自动检测导致的编码错误
            # SSE流应该使用UTF-8编码，特别是包含JSON数据时
            response.encoding = "utf-8"

            # Store the connection for later cleanup
            self.sse_connection = response

            self.connection_established = True

            # SSE事件状态
            current_event_type = None
            current_data_lines = []

            for line in response.iter_lines(decode_unicode=True):
                # 检查连接是否仍然有效
                if not self.connection_established:
                    break

                # Skip empty lines
                if not line:
                    # 空行表示一个SSE事件结束，处理累积的数据
                    if current_event_type and current_data_lines:
                        # 根据SSE规范，多个data行应该用换行符连接
                        # iter_lines已经去除了行尾的换行符，所以我们需要手动添加
                        # 如果只有一个data行，直接使用；多个data行用换行符连接
                        if len(current_data_lines) == 1:
                            data = current_data_lines[0]
                        else:
                            data = "\n".join(current_data_lines)

                        # 根据事件类型调用相应的回调函数
                        if current_event_type == "endpoint":
                            self.sse_endpoint = data.strip()
                            if self.on_endpoint:
                                self.on_endpoint(self.sse_endpoint)
                        elif current_event_type == "message":
                            if self.on_message:
                                self.on_message(data)
                        elif current_event_type == "ping":
                            # 忽略ping事件，这是keep-alive消息
                            pass
                        else:
                            # 未知事件类型，可以记录日志
                            print(
                                f"[{self.logger_name}]: Unknown event type: {current_event_type}"
                            )

                    # 重置状态
                    current_event_type = None
                    current_data_lines = []
                    continue

                if line.startswith("event: "):
                    current_event_type = line[
                        7:
                    ].strip()  # Remove "event: " prefix and strip whitespace
                elif line.startswith("data: "):
                    data_line = line[6:]  # Remove "data: " prefix
                    current_data_lines.append(data_line)

        except (AttributeError, ConnectionError, OSError) as e:
            # 连接关闭相关的异常，这是正常的（当连接被关闭时）
            if self.connection_established:
                # 只有在连接仍然建立时才记录错误（避免重复日志）
                error_msg = str(e)
                if "read" in error_msg.lower() or "closed" in error_msg.lower():
                    # 连接关闭是正常的，不需要记录为错误
                    print(f"[{self.logger_name}]: SSE connection closed")
                else:
                    print(f"[{self.logger_name}]: SSE listener error: {e}")
        except Exception as e:
            # 其他异常
            print(f"[{self.logger_name}]: SSE listener error: {e}")
            if self.on_error:
                self.on_error(e)
        finally:
            # 清理连接状态
            self.connection_established = False
            if self.sse_connection:
                try:
                    self.sse_connection.close()
                except:
                    pass
                self.sse_connection = None
