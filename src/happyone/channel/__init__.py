"""通道层：移动端与电脑之间的取件码配对、WebRTC 信令路由。

对外入口：
- ``happyone.channel.router.router``：APIRouter（HTTP + WebSocket），由骨架线挂载；
- ``happyone.channel.service.PairingService``：纯逻辑实现，不依赖 FastAPI；
- ``happyone.channel.models``：请求 / 响应模型、事件名、错误文案。
"""

__all__: list[str] = []
