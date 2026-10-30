"""`python -m spending_mapping`：默认跑常驻拉取服务；带子命令时走 CLI。"""

from __future__ import annotations

import logging
import sys

from .config import MailPullConfig
from .service import run_forever


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if argv:
        from .cli import dispatch

        return dispatch(argv)

    try:
        cfg = MailPullConfig.from_env()
    except RuntimeError as exc:
        logging.error("%s", exc)
        return 1
    return run_forever(cfg)


if __name__ == "__main__":
    sys.exit(main())
