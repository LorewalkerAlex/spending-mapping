"""`python -m spending_mapping`：运行常驻拉取服务。"""

from __future__ import annotations

import logging
import sys

from .config import MailPullConfig
from .service import run_forever


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        cfg = MailPullConfig.from_env()
    except RuntimeError as exc:
        logging.error("%s", exc)
        return 1
    return run_forever(cfg)


if __name__ == "__main__":
    sys.exit(main())
