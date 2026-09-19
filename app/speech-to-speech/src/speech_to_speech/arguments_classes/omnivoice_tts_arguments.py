from dataclasses import dataclass, field
from typing import Optional


@dataclass
class OmniVoiceTTSHandlerArguments:
    omnivoice_base_url: str = field(
        default="http://127.0.0.1:8791",
        metadata={"help": "旁挂 OmniVoice 服务地址（app/omnivoice/server.py 启动的那个）。"},
    )
    omnivoice_voice: Optional[str] = field(
        default=None,
        metadata={"help": "音色目录名，对应 app/voices/<音色名>/。留空则用服务端默认。"},
    )
    omnivoice_speed: Optional[float] = field(
        default=None,
        metadata={"help": "语速，1.0 为模型默认。OmniVoice 偏快，陪聊 0.75~0.9 更自然。"},
    )
    omnivoice_blocksize: int = field(
        default=1024,
        metadata={"help": "输出音频分块大小（采样点）。"},
    )
    omnivoice_timeout: float = field(
        default=120.0,
        metadata={"help": "单次合成请求的超时秒数。"},
    )
