"""echo：最小可用工具，用来证明「注册 → 校验 → 执行 → 响应」这条链路通了。

它同时是 422 验收的载体：``times`` 有范围约束，传 0 或 99 必须被拦下。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from driver_platform.tools.base import Tool


class EchoInput(BaseModel):
    message: str = Field(min_length=1, description="要回显的文本")
    times: int = Field(default=1, ge=1, le=5, description="重复次数，1–5")


class EchoOutput(BaseModel):
    echo: str = Field(description="重复 times 次后的文本，用换行分隔")
    times: int = Field(description="实际重复次数")


class EchoTool(Tool[EchoInput, EchoOutput]):
    name = "echo"
    contract_version = "1.0"
    description = "把 message 回显 times 次（示例工具，用于验证调用链路）"
    input_model = EchoInput
    output_model = EchoOutput

    async def execute(self, arguments: EchoInput) -> EchoOutput:
        return EchoOutput(
            echo="\n".join([arguments.message] * arguments.times),
            times=arguments.times,
        )
