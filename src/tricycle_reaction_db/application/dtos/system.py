"""
Author: TMJ
Date: 2026-07-12 21:03:50
LastEditors: TMJ
LastEditTime: 2026-07-12 21:43:30
Description: 请填写简介
"""

from pydantic import BaseModel, ConfigDict


class SystemInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    version: str
    environment: str
