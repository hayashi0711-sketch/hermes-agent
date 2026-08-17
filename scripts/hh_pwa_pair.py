#!/usr/bin/env python
"""H-H Agent: 動的ペアリングコード（`hh pwa pair`の実体）を発行するCLI。

Phase1a spec §7.1/§7.1b の「スマホ再ペアリング」手順の実体。初回ペアリング
用の静的コード（`HH_PAIRING_CODE` Secret）は `bootstrap_done` が一度立つと
恒久的に使えなくなるため、再ペアリングが必要になった時にこのCLIから
動的コード（`pairing_offer:`）を発行する。

契約:
    - 本番 Modal の hh-agent-approvals Dict へ pairing_offer レコードを
      書き込むため、ローカルに `modal token` 認証済みでなければ
      `store` アクセスで失敗する。
    - コードは標準出力（画面）にのみ表示する。ログファイルへは絶対に
      書かない（Phase1a spec §7.1 手順1「コードを端末の画面に表示」）。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from modal_hub.core import security, store  # noqa: E402


def _reconfigure_stdout() -> None:
    """Windows 端末での文字化け対策（`hh_issue_agent_token.py` の `_log()` と同じ）。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass


def main() -> int:
    _reconfigure_stdout()
    now = time.time()
    try:
        code = security.create_pairing_offer(store, now=now)
    except security.SecurityError as exc:
        # sha256 衝突（事実上起きないが仕様上の例外）。画面にのみ出して非ゼロ終了。
        print(f"[hh_pwa_pair] ペアリングコードの発行に失敗しました: {exc}", file=sys.stderr)
        return 1
    except store.StoreError:
        # store.StoreError のメッセージには `pairing_offer:<sha256(code)>` の
        # キー（= 発行したコードのハッシュ）が埋め込まれる（store.py の
        # put_if_absent 実装参照）。8桁数字は総当たりで元コードを容易に復元
        # できるため、例外本文をそのまま出すと「コードは画面にのみ表示、
        # ログに残さない」という仕様（Phase1a spec §7.1手順1）を実質的に
        # 破る。固定文言のみをstderrへ出し、詳細は出さない。
        print(
            "[hh_pwa_pair] ペアリングコードの発行に失敗しました（ストア書き込みエラー）。"
            "しばらくしてから再試行してください。",
            file=sys.stderr,
        )
        return 1

    expires_at = now + security.PAIRING_OFFER_TTL_SECONDS
    expires_hm = time.strftime("%H:%M:%S", time.localtime(expires_at))
    minutes = security.PAIRING_OFFER_TTL_SECONDS // 60
    print(f"ペアリングコード: {code}")
    print(f"有効期限: {expires_hm}まで有効 ({minutes}分間)")
    print("スマホのPWA画面でこのコードを入力してください。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
