"""应用入口：装配编排器与 HTTP 端点，供 uvicorn 启动。

启动方式：``uvicorn weiliao_agent.main:app --reload``。
Story 1.4 的 ``/health`` 日后挂同一 app。
"""

from weiliao_agent.adapters.http import create_app
from weiliao_agent.pipeline import PipelineOrchestrator

app = create_app(PipelineOrchestrator())
