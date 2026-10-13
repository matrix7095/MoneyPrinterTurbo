"""Application configuration - root APIRouter

Defines all FastAPI application endpoints.

Resources:
    1. https://fastapi.tiangolo.com/tutorial/bigger-applications

"""

from fastapi import APIRouter

from app.controllers import ping
from app.controllers.v1 import llm, video
from app.controllers.v1 import agent, content, content_execution, discovery, deployment

root_api_router = APIRouter()
root_api_router.include_router(ping.router)

# v1
root_api_router.include_router(video.router)
root_api_router.include_router(llm.router)
root_api_router.include_router(agent.router)
root_api_router.include_router(discovery.router)
root_api_router.include_router(content.router)
root_api_router.include_router(content_execution.router)
root_api_router.include_router(deployment.router)
