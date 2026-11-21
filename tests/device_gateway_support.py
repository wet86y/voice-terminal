"""ASGI（异步服务接口）测试的网关端口交接；不启动产品网络监听。"""
import socket


def configure_test_gateway(runtime) -> int:
    """给纯 ASGI（异步服务接口）夹具模拟一个本轮实际端口，沿用生产端口门禁。"""
    # 这里只模拟服务器传给应用的地址，释放后不会按此端口重新绑定监听器。
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    runtime._device_gateway_port = port
    return port
