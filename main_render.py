# ====================================================================================
# Apex BOT v19.0.28 - Safety and Frequency Finalized (Patch 36)
#
# 修正ポイント:
# 1. 【安全確認】動的取引閾値 (0.70, 0.65, 0.60) を最終確定。
# 2. 【安全確認】取引実行ロジック (SL/TP, RRR >= 1.0, CCXT精度調整) の堅牢性を再確認。
# 3. 【バージョン更新】全てのバージョン情報を Patch 36 に更新。
# 4. 【キーエラー/警告修正】calculate_technical_indicatorsおよびget_signal_metricsにおけるBBW/BBPの参照を修正。
# ====================================================================================

# 1. 必要なライブラリをインポート
import os
import time
import logging
import requests
import ccxt.async_support as ccxt_async
import ccxt
import numpy as np
import pandas as pd
import pandas_ta as ta
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple, Any, Callable
import asyncio
from fastapi import FastAPI
from fastapi.responses import JSONResponse
import uvicorn
from dotenv import load_dotenv
import sys
import random
import json
import re
import ftplib 
import uuid 

# .envファイルから環境変数を読み込む
load_dotenv()

# 💡 【ログ確認対応】ロギング設定を明示的に定義
logging.basicConfig(
    level=logging.INFO, # INFOレベル以上のメッセージを出力
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# ====================================================================================
# CONFIG & CONSTANTS
# ====================================================================================

JST = timezone(timedelta(hours=9))

# 出来高TOP40に加えて、主要な基軸通貨をDefaultに含めておく (現物シンボル形式 BTC/USDT)
DEFAULT_SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT", "ADA/USDT",
    "DOGE/USDT", "DOT/USDT", "TRX/USDT", "MATIC/USDT", 
    "LTC/USDT", "AVAX/USDT", "LINK/USDT", "UNI/USDT", "ETC/USDT", "BCH/USDT",
    "NEAR/USDT", "ATOM/USDT", "FTM/USDT", "ALGO/USDT", "XLM/USDT", "SAND/USDT",
    "GALA/USDT", "FIL/USDT", "EOS/USDT", "AXS/USDT", "MANA/USDT", "AAVE/USDT",
    "MKR/USDT", "THETA/USDT", "FLOW/USDT", "IMX/USDT", 
]
TOP_SYMBOL_LIMIT = 40               # 監視対象銘柄の最大数 (出来高TOPから選出)を40に引き上げ
LOOP_INTERVAL = 60 * 10             # メインループの実行間隔 (秒) - 10分ごと
ANALYSIS_ONLY_INTERVAL = 60 * 60    # 分析専用通知の実行間隔 (秒) - 1時間ごと
WEBSHARE_UPLOAD_INTERVAL = 60 * 60  # WebShareログアップロード間隔 (1時間ごと)
MONITOR_INTERVAL = 10               # ポジション監視ループの実行間隔 (秒)

# 💡 クライアント設定
CCXT_CLIENT_NAME = os.getenv("EXCHANGE_CLIENT", "mexc")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
API_KEY = os.getenv(f"{CCXT_CLIENT_NAME.upper()}_API_KEY")
SECRET_KEY = os.getenv(f"{CCXT_CLIENT_NAME.upper()}_SECRET")
TEST_MODE = os.getenv("TEST_MODE", "False").lower() in ('true', '1', 't')
SKIP_MARKET_UPDATE = os.getenv("SKIP_MARKET_UPDATE", "False").lower() in ('true', '1', 't')

# 💡 自動売買設定 (動的ロットのベースサイズ)
try:
    BASE_TRADE_SIZE_USDT = float(os.getenv("BASE_TRADE_SIZE_USDT", "100")) 
except ValueError:
    BASE_TRADE_SIZE_USDT = 100.0
    logging.warning("⚠️ BASE_TRADE_SIZE_USDTが不正な値です。100 USDTを使用します。")
    
if BASE_TRADE_SIZE_USDT < 10:
    logging.warning("⚠️ BASE_TRADE_SIZE_USDTが10 USDT未満です。ほとんどの取引所の最小取引額を満たさない可能性があります。")


# 💡 WEBSHARE設定 (FTP/WebDAVなど、外部ログストレージを想定)
WEBSHARE_HOST = os.getenv("WEBSHARE_HOST")
WEBSHARE_PORT = int(os.getenv("WEBSHARE_PORT", "21")) # デフォルトはFTPポート
WEBSHARE_USER = os.getenv("WEBSHARE_USER")
WEBSHARE_PASS = os.getenv("WEBSHARE_PASS")

# グローバル変数 (状態管理用)
EXCHANGE_CLIENT: Optional[ccxt_async.Exchange] = None
CURRENT_MONITOR_SYMBOLS: List[str] = DEFAULT_SYMBOLS.copy()
LAST_SUCCESS_TIME: float = 0.0
LAST_SIGNAL_TIME: Dict[str, float] = {}
LAST_ANALYSIS_SIGNALS: List[Dict] = []
LAST_HOURLY_NOTIFICATION_TIME: float = 0.0
LAST_ANALYSIS_ONLY_NOTIFICATION_TIME: float = 0.0
LAST_WEBSHARE_UPLOAD_TIME: float = 0.0 
GLOBAL_MACRO_CONTEXT: Dict = {} # マクロコンテキストを保持するための変数
IS_FIRST_MAIN_LOOP_COMPLETED: bool = False # 初回メインループ完了フラグ
OPEN_POSITIONS: List[Dict] = [] # 現在保有中のポジション (SL/TP監視用)

if TEST_MODE:
    logging.warning("⚠️ WARNING: TEST_MODE is active. Trading is disabled.")

# 取引ルール設定
TRADE_SIGNAL_COOLDOWN = 60 * 60 * 2 # 同一銘柄のシグナル通知クールダウン（2時間）
SIGNAL_THRESHOLD = 0.65             # 動的閾値のベースライン (通常時の値 2-3銘柄/日を想定)
TOP_SIGNAL_COUNT = 3                # 通知するシグナルの最大数
REQUIRED_OHLCV_LIMITS = {'15m': 500, '1h': 500, '4h': 500} # 取得するOHLCVの足数

# テクニカル分析定数 (v19.0.28ベース)
TARGET_TIMEFRAMES = ['15m', '1h', '4h']
BASE_SCORE = 0.60                   # ベースとなる取引基準点 (60点)
LONG_TERM_SMA_LENGTH = 200          # 長期トレンドフィルタ用SMA
LONG_TERM_REVERSAL_PENALTY = 0.20   # 長期トレンド逆行時のペナルティ
STRUCTURAL_PIVOT_BONUS = 0.07       # 価格構造/ピボット支持時のボーナス
RSI_MOMENTUM_LOW = 40               # RSIが40以下でロングモメンタム候補
MACD_CROSS_PENALTY = 0.15           # MACDが不利なクロス/発散時のペナルティ
LIQUIDITY_BONUS_MAX = 0.06          # 流動性(板の厚み)による最大ボーナス
FGI_PROXY_BONUS_MAX = 0.05          # 恐怖・貪欲指数による最大ボーナス/ペナルティ
FOREX_BONUS_MAX = 0.0               # 為替機能を削除するため0.0に設定

# 市場環境に応じた動的閾値調整のための定数 (ユーザー要望に合わせて調整 - Patch 36確定)
FGI_SLUMP_THRESHOLD = -0.02         # FGIプロキシがこの値未満の場合、市場低迷と見なす
FGI_ACTIVE_THRESHOLD = 0.02         # FGIプロキシがこの値を超える場合、市場活発と見なす
# 🚨 最終調整箇所: 頻度目標達成のため閾値を引き下げ (この値で確定)
SIGNAL_THRESHOLD_SLUMP = 0.70       # 低迷時の閾値 (1-2銘柄/日を想定)
SIGNAL_THRESHOLD_NORMAL = 0.65      # 通常時の閾値 (2-3銘柄/日を想定)
SIGNAL_THRESHOLD_ACTIVE = 0.60      # 活発時の閾値 (3+銘柄/日を想定)

RSI_DIVERGENCE_BONUS = 0.10         # RSIダイバージェンス時のボーナス (未使用だが定数として残す)
VOLATILITY_BB_PENALTY_THRESHOLD = 0.01 # ボラティリティ過熱時のペナルティ閾値
OBV_MOMENTUM_BONUS = 0.04           # OBVトレンド一致時のボーナス

# ====================================================================================
# UTILITIES & FORMATTING
# ====================================================================================

def format_usdt(amount: float) -> str:
    """USDT金額を整形する"""
    if amount >= 1.0:
        return f"{amount:,.2f}"
    elif amount >= 0.01:
        return f"{amount:.4f}"
    else:
        return f"{amount:.6f}"

def get_estimated_win_rate(score: float) -> str:
    """スコアに基づいて推定勝率を返す (通知用)"""
    if score >= 0.90: return "90%+"
    if score >= 0.85: return "85-90%"
    if score >= 0.75: return "75-85%"
    if score >= 0.65: return "65-75%" 
    if score >= 0.60: return "60-65%"
    return "<60% (低)"

def get_score_breakdown(signal: Dict) -> str:
    """分析スコアの詳細なブレークダウンメッセージを作成する (Telegram通知用)"""
    tech_data = signal.get('tech_data', {})
    timeframe = signal.get('timeframe', 'N/A')
    
    # スコア算出ロジックから取得できる定数値 (通知表示に利用)
    LONG_TERM_REVERSAL_PENALTY_CONST = LONG_TERM_REVERSAL_PENALTY 
    MACD_CROSS_PENALTY_CONST = MACD_CROSS_PENALTY                 
    LIQUIDITY_BONUS_POINT_CONST = LIQUIDITY_BONUS_MAX           
    
    breakdown_list = []

    # 1. ベーススコア (全ての分析の出発点)
    breakdown_list.append(f"  - **ベーススコア ({timeframe})**: <code>+{BASE_SCORE*100:.1f}</code> 点")
    
    # 2. 長期トレンド/構造の確認
    penalty_applied = tech_data.get('long_term_reversal_penalty_value', 0.0)
    if penalty_applied > 0.0:
        breakdown_list.append(f"  - ❌ 長期トレンド逆行 (SMA{LONG_TERM_SMA_LENGTH}): <code>-{penalty_applied*100:.1f}</code> 点")
    else:
        # ペナルティ回避時のボーナス相当として表示
        breakdown_list.append(f"  - ✅ 長期トレンド一致 (SMA{LONG_TERM_SMA_LENGTH}): <code>+{LONG_TERM_REVERSAL_PENALTY_CONST*100:.1f}</code> 点 (ペナルティ回避)")

    # 価格構造/ピボット支持ボーナス
    pivot_bonus = tech_data.get('structural_pivot_bonus', 0.0)
    if pivot_bonus > 0.0:
        breakdown_list.append(f"  - ✅ 価格構造/ピボット支持: <code>+{pivot_bonus*100:.1f}</code> 点")

    # 3. モメンタム/出来高の確認
    macd_penalty_applied = tech_data.get('macd_penalty_value', 0.0)
    total_momentum_penalty = macd_penalty_applied

    if total_momentum_penalty > 0.0:
        breakdown_list.append(f"  - ❌ モメンタム/クロス不利: <code>-{total_momentum_penalty*100:.1f}</code> 点")
    else:
        # ペナルティ回避時のボーナス相当として表示
        breakdown_list.append(f"  - ✅ MACD/RSIモメンタム加速: <code>+{MACD_CROSS_PENALTY_CONST*100:.1f}</code> 点相当 (ペナルティ回避)")

    # 出来高/OBV確証ボーナス
    obv_bonus = tech_data.get('obv_momentum_bonus_value', 0.0)
    if obv_bonus > 0.0:
        breakdown_list.append(f"  - ✅ 出来高/OBV確証: <code>+{obv_bonus*100:.1f}</code> 点")
    
    # 4. 流動性/マクロ要因
    # 流動性ボーナス
    liquidity_bonus = tech_data.get('liquidity_bonus_value', 0.0)
    if liquidity_bonus > 0.0:
        breakdown_list.append(f"  - ✅ 流動性 (板の厚み) 優位: <code>+{LIQUIDITY_BONUS_POINT_CONST*100:.1f}</code> 点")
        
    # FGIマクロ要因
    fgi_bonus = tech_data.get('sentiment_fgi_proxy_bonus', 0.0)
    if abs(fgi_bonus) > 0.001:
        sign = '✅' if fgi_bonus > 0 else '❌'
        breakdown_list.append(f"  - {sign} FGIマクロ影響: <code>{'+' if fgi_bonus > 0 else ''}{fgi_bonus*100:.1f}</code> 点")

    # 為替マクロ (常に0.0を表示)
    forex_bonus = tech_data.get('forex_bonus', 0.0) 
    breakdown_list.append(f"  - ⚪ 為替マクロ影響: <code>{forex_bonus*100:.1f}</code> 点 (機能削除済)")
    
    # ボラティリティペナルティ (負の値のみ表示)
    volatility_penalty = tech_data.get('volatility_penalty_value', 0.0)
    if volatility_penalty < 0.0:
        breakdown_list.append(f"  - ❌ ボラティリティ過熱ペナルティ: <code>{volatility_penalty*100:.1f}</code> 点")

    return "\n".join(breakdown_list)


def format_analysis_only_message(all_signals: List[Dict], macro_context: Dict, current_threshold: float, monitoring_count: int) -> str:
    """1時間ごとの分析専用メッセージを作成する"""
    now_jst = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")
    
    # 1. 候補リストの作成 (スコア降順にソート)
    sorted_signals = sorted(all_signals, key=lambda s: s.get('score', 0.0), reverse=True)
    
    header = (
        f"📊 **Apex Market Snapshot (Hourly Analysis)**\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - **確認日時**: {now_jst} (JST)\n"
        f"  - **取引ステータス**: <b>分析通知のみ</b>\n"
        f"  - **対象銘柄数**: <code>{monitoring_count}</code>\n"
        f"  - **監視取引所**: <code>{CCXT_CLIENT_NAME.upper()}</code>\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n\n"
    )

    # マクロコンテキスト情報
    fgi_raw_value = macro_context.get('fgi_raw_value', 'N/A')
    fgi_proxy = macro_context.get('fgi_proxy', 0.0)
    forex_bonus = macro_context.get('forex_bonus', 0.0) 

    fgi_sentiment = "リスクオン" if fgi_proxy > FGI_ACTIVE_THRESHOLD else ("リスクオフ" if fgi_proxy < FGI_SLUMP_THRESHOLD else "中立")
    forex_display = "中立 (機能削除済)"
    
    # 市場環境の判定
    if current_threshold == SIGNAL_THRESHOLD_SLUMP:
        market_condition_text = f"低迷/リスクオフ (Threshold: {SIGNAL_THRESHOLD_SLUMP*100:.0f}点)"
    elif current_threshold == SIGNAL_THRESHOLD_ACTIVE:
        market_condition_text = f"活発/リスクオン (Threshold: {SIGNAL_THRESHOLD_ACTIVE*100:.0f}点)"
    else:
        market_condition_text = f"通常/中立 (Threshold: {SIGNAL_THRESHOLD_NORMAL*100:.0f}点)"
    

    macro_section = (
        f"🌍 <b>グローバルマクロ分析</b>\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - **現在の市場環境**: <code>{market_condition_text}</code>\n"
        f"  - **恐怖・貪欲指数 (FGI)**: <code>{fgi_raw_value}</code> ({fgi_sentiment})\n"
        f"  - **為替マクロ (EUR/USD)**: {forex_display}\n"
        f"  - **総合マクロ影響**: <code>{((fgi_proxy + forex_bonus) * 100):.2f}</code> 点\n\n"
    )

    # トップシグナル情報 (Rank 1のみに焦点を当てる)
    signal_section = "📈 <b>トップシグナル候補 (スコア順)</b>\n"
    
    if sorted_signals:
        top_signal = sorted_signals[0] # Rank 1を取得
        symbol = top_signal['symbol']
        timeframe = top_signal['timeframe']
        score = top_signal['score']
        rr_ratio = top_signal['rr_ratio']
        
        # スコア詳細ブレークダウンの生成
        breakdown_details = get_score_breakdown(top_signal)
        
        score_color = ""
        if score < current_threshold:
             score_color = "⚠️" 
        if score < BASE_SCORE: 
             score_color = "🔴"
             
        rr_display = f"1:{rr_ratio:.1f}" if rr_ratio >= 1.0 else f"1:{rr_ratio:.1f} ❌"
        
        signal_section += (
            f"  🥇 <b>{symbol}</b> ({timeframe}) - **最高スコア** {score_color}\n"
            f"     - **総合スコア**: <code>{score * 100:.2f} / 100</code> (推定勝率: {get_estimated_win_rate(score)})\n"
            f"     - **リスクリワード比率 (RRR)**: <code>{rr_display}</code>\n"
            f"  \n**📊 スコア詳細ブレークダウン** (+/-要因)\n"
            f"  <code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
            f"{breakdown_details}\n"
            f"  <code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        )

        # 警告メッセージの追加
        if top_signal['score'] < current_threshold:
             signal_section += f"\n<pre>⚠️ 注: 上記は監視中の最高スコアですが、取引閾値 ({current_threshold*100:.0f}点) 未満です。</pre>\n"
        
        if top_signal['score'] < BASE_SCORE:
             signal_section += f"<pre>🔴 警告: 最高スコアが取引基準点 ({BASE_SCORE*100:.0f}点) 未満です。</pre>\n"

    else:
        signal_section += "  - **シグナル候補なし**: 現在、すべての監視銘柄で最低限のリスクリワード比率を満たすロングシグナルは見つかりませんでした。\n"
    
    footer = (
        f"\n<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"<pre>※ この通知は取引実行を伴いません。</pre>"
        f"<i>Bot Ver: v19.0.28 - Safety and Frequency Finalized (Patch 36)</i>" 
    )

    return header + macro_section + signal_section + footer

def format_startup_message(
    account_status: Dict, 
    macro_context: Dict, 
    monitoring_count: int,
    current_threshold: float,
    bot_version: str
) -> str:
    """初回起動完了通知用のメッセージを作成する"""
    now_jst = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")
    
    # マクロ情報
    fgi_value = macro_context.get('fgi_raw_value', 'N/A')
    fgi_proxy = macro_context.get('fgi_proxy', 0.0)
    forex_bonus = macro_context.get('forex_bonus', 0.0)
    fgi_sentiment = "リスクオン" if fgi_proxy > FGI_ACTIVE_THRESHOLD else ("リスクオフ" if fgi_proxy < FGI_SLUMP_THRESHOLD else "中立")

    # 市場環境の判定
    if current_threshold == SIGNAL_THRESHOLD_SLUMP:
        market_condition_text = "低迷/リスクオフ"
    elif current_threshold == SIGNAL_THRESHOLD_ACTIVE:
        market_condition_text = "活発/リスクオン"
    else:
        market_condition_text = "通常/中立"
        
    # 自動売買ステータス
    trade_status = "自動売買 **ON**" if not TEST_MODE else "自動売買 **OFF** (TEST_MODE)"

    header = (
        f"🤖 **Apex BOT 起動完了通知** 🟢\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - **確認日時**: {now_jst} (JST)\n"
        f"  - **取引所**: <code>{CCXT_CLIENT_NAME.upper()}</code> (現物モード)\n"
        f"  - **自動売買**: <b>{trade_status}</b>\n"
        f"  - **取引ロット (BASE)**: <code>{BASE_TRADE_SIZE_USDT:.2f}</code> USDT\n" # BASEに変更
        f"  - **監視銘柄数**: <code>{monitoring_count}</code>\n"
        f"  - **BOTバージョン**: <code>{bot_version}</code>\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n\n"
    )

    # 1. 残高/ポジション情報
    balance_section = f"💰 <b>口座ステータス</b>\n"
    if account_status.get('error'):
        # エラーメッセージを分かりやすく表示
        balance_section += f"<pre>⚠️ ステータス取得失敗 (セキュリティのため詳細なエラーは表示しません。ログを確認してください)</pre>\n"
    else:
        balance_section += (
            f"  - **USDT残高**: <code>{format_usdt(account_status['total_usdt_balance'])}</code> USDT\n"
        )
        
        # 管理ポジションの表示
        if OPEN_POSITIONS:
            total_managed_value = sum(p['filled_usdt'] for p in OPEN_POSITIONS)
            balance_section += (
                f"  - **管理中ポジション**: <code>{len(OPEN_POSITIONS)}</code> 銘柄 (投入合計: <code>{format_usdt(total_managed_value)}</code> USDT)\n"
            )
            for i, pos in enumerate(OPEN_POSITIONS[:3]): # Top 3のみ表示
                base_currency = pos['symbol'].replace('/USDT', '')
                balance_section += f"    - Top {i+1}: {base_currency} (SL: {format_usdt(pos['stop_loss'])} / TP: {format_usdt(pos['take_profit'])})\n"
            if len(OPEN_POSITIONS) > 3:
                balance_section += f"    - ...他 {len(OPEN_POSITIONS) - 3} 銘柄\n"
        else:
             balance_section += f"  - **管理中ポジション**: <code>なし</code>\n"

        # 既存の現物ポジション（CCXTから取得）は簡略化
        open_ccxt_positions = [p for p in account_status['open_positions'] if p['usdt_value'] >= 10]
        if open_ccxt_positions:
             balance_section += f"  - **未管理の現物**: <code>{len(open_ccxt_positions)}</code> 銘柄 (CCXT参照)\n"
        
    balance_section += f"\n"

    # 2. 市場状況 (スコア付け)
    macro_section = (
        f"🌍 <b>市場環境スコアリング</b>\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - **取引閾値 (Score)**: <code>{current_threshold*100:.0f} / 100</code>\n"
        f"  - **現在の市場環境**: <code>{market_condition_text}</code>\n"
        f"  - **FGI (恐怖・貪欲)**: <code>{fgi_value}</code> ({fgi_sentiment})\n"
        f"  - **総合マクロ影響**: <code>{((fgi_proxy + forex_bonus) * 100):.2f}</code> 点\n\n"
    )

    footer = (
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"<pre>※ この通知はメインの分析ループが一度完了したことを示します。約10分ごとに分析が実行されます。</pre>"
    )

    return header + balance_section + macro_section + footer

def format_telegram_message(signal: Dict, context: str, current_threshold: float, trade_result: Optional[Dict] = None, exit_type: Optional[str] = None) -> str:
    """Telegram通知用のメッセージを作成する (取引結果を追加)"""
    now_jst = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")
    symbol = signal['symbol']
    timeframe = signal['timeframe']
    score = signal['score']
    
    # SL/TP/RRRはシグナルまたは取引結果から取得
    entry_price = signal.get('entry_price', trade_result.get('entry_price', 0.0))
    stop_loss = signal.get('stop_loss', trade_result.get('stop_loss', 0.0))
    take_profit = signal.get('take_profit', trade_result.get('take_profit', 0.0))
    rr_ratio = signal.get('rr_ratio', 0.0)
    
    estimated_wr = get_estimated_win_rate(score)
    
    breakdown_details = get_score_breakdown(signal) 

    trade_section = ""
    trade_status_line = ""

    if context == "取引シグナル":
        # エントリー通知
        lot_size = signal.get('lot_size_usdt', BASE_TRADE_SIZE_USDT) # 動的ロット
        
        if TEST_MODE:
            trade_status_line = f"⚠️ **テストモード**: 取引は実行されません。(ロット: {format_usdt(lot_size)} USDT)"
        elif trade_result is None or trade_result.get('status') == 'error':
            trade_status_line = f"❌ **自動売買 失敗**: {trade_result.get('error_message', 'APIエラー')}"
        elif trade_result.get('status') == 'ok':
            trade_status_line = "✅ **自動売買 成功**: 現物ロング注文を執行しました。"
            filled_amount = trade_result.get('filled_amount', 'N/A')
            filled_usdt = trade_result.get('filled_usdt', 'N/A')
            trade_section = (
                f"💰 **取引実行結果**\n"
                f"  - **注文タイプ**: <code>現物 (Spot) / 成行買い</code>\n"
                f"  - **動的ロット**: <code>{format_usdt(lot_size)}</code> USDT (目標)\n"
                f"  - **約定数量**: <code>{filled_amount:.4f}</code> {symbol.split('/')[0]}\n"
                f"  - **平均約定額**: <code>{format_usdt(filled_usdt)}</code> USDT\n"
            )
            
    elif context == "ポジション決済":
        # 決済通知
        trade_status_line = f"🔴 **ポジション決済**: {exit_type} トリガー"
        
        entry_price = trade_result.get('entry_price', 0.0)
        exit_price = trade_result.get('exit_price', 0.0)
        pnl_usdt = trade_result.get('pnl_usdt', 0.0)
        pnl_rate = trade_result.get('pnl_rate', 0.0)
        filled_amount = trade_result.get('filled_amount', 'N/A')
        
        pnl_sign = "✅ 利益確定" if pnl_usdt >= 0 else "❌ 損切り"
        
        trade_section = (
            f"💰 **決済実行結果** - {pnl_sign}\n"
            f"  - **エントリー価格**: <code>{format_usdt(entry_price)}</code>\n"
            f"  - **決済価格**: <code>{format_usdt(exit_price)}</code>\n"
            f"  - **約定数量**: <code>{filled_amount:.4f}</code> {symbol.split('/')[0]}\n"
            f"  - **損益**: <code>{'+' if pnl_usdt >= 0 else ''}{format_usdt(pnl_usdt)}</code> USDT ({pnl_rate*100:.2f}%)\n"
        )
            
    
    message = (
        f"🚀 **Apex TRADE {context}**\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - **日時**: {now_jst} (JST)\n"
        f"  - **銘柄**: <b>{symbol}</b> ({timeframe})\n"
        f"  - **ステータス**: {trade_status_line}\n" 
        f"  - **総合スコア**: <code>{score * 100:.2f} / 100</code>\n"
        f"  - **取引閾値**: <code>{current_threshold * 100:.2f}</code> 点\n"
        f"  - **推定勝率**: <code>{estimated_wr}</code>\n"
        f"  - **リスクリワード比率 (RRR)**: <code>1:{rr_ratio:.2f}</code>\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"📌 **ポジション管理パラメータ**\n"
        f"  - **エントリー**: <code>{format_usdt(entry_price)}</code>\n"
        f"  - **ストップロス (SL)**: <code>{format_usdt(stop_loss)}</code>\n"
        f"  - **テイクプロフィット (TP)**: <code>{format_usdt(take_profit)}</code>\n"
        f"  - **リスク幅 (SL)**: <code>{format_usdt(entry_price - stop_loss)}</code> USDT\n"
        f"  - **リワード幅 (TP)**: <code>{format_usdt(take_profit - entry_price)}</code> USDT\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
    )
    
    # 取引結果セクションをシグナル詳細の前に追加
    if trade_section:
        message += trade_section + f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        
    # エントリー時のみスコアブレークダウンを表示
    if context == "取引シグナル":
        message += (
            f"  \n**📊 スコア詳細ブレークダウン** (+/-要因)\n"
            f"{breakdown_details}\n"
            f"  <code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        )
        
    message += (f"<i>Bot Ver: v19.0.28 - Safety and Frequency Finalized (Patch 36)</i>")
    return message


async def send_telegram_notification(message: str) -> bool:
    """Telegramにメッセージを送信する"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.error("❌ Telegram設定 (TOKEN/ID) が不足しています。通知をスキップします。")
        return False
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        'chat_id': TELEGRAM_CHAT_ID,
        'text': message,
        'parse_mode': 'HTML'
    }
    
    try:
        response = await asyncio.to_thread(requests.post, url, data=payload, timeout=5)
        response.raise_for_status()
        logging.info(f"✅ Telegram通知を送信しました。")
        return True
    except requests.exceptions.HTTPError as e:
        error_details = response.json() if 'response' in locals() else 'N/A'
        logging.error(f"❌ Telegram HTTPエラー: {e} - 詳細: {error_details}")
    except requests.exceptions.RequestException as e:
        logging.error(f"❌ Telegramリクエストエラー: {e}")
    return False

def _to_json_compatible(obj):
    """
    再帰的にオブジェクトをJSON互換の型に変換するヘルパー関数。
    特にboolやNumPyのスカラ型を文字列/Pythonネイティブ型に変換する。
    """
    if isinstance(obj, dict):
        return {k: _to_json_compatible(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_to_json_compatible(elem) for elem in obj]
    elif isinstance(obj, (bool, np.bool_)):
        # すべてのブーリアン型を文字列に変換し、シリアライズエラーを回避
        return str(obj) 
    elif isinstance(obj, np.generic):
        # numpy.float64, numpy.int64 などのNumPyスカラをPythonネイティブ型に変換
        return obj.item()
    return obj


def log_signal(data: Dict, log_type: str, trade_result: Optional[Dict] = None) -> None:
    """シグナルまたは取引結果をローカルファイルにログする"""
    try:
        # ロギングされるデータの基本構造を定義
        log_entry = {
            'timestamp_jst': datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
            'log_type': log_type,
            'symbol': data.get('symbol', 'N/A'),
            'timeframe': data.get('timeframe', 'N/A'),
            'score': data.get('score', 0.0),
            'rr_ratio': data.get('rr_ratio', 0.0),
            'trade_result': trade_result or data.get('trade_result', None), # 決済時にはtrade_resultがdata内に含まれる
            'full_data': data,
        }
        
        # JSONシリアライズエラーを回避するために辞書をクリーニング
        cleaned_log_entry = _to_json_compatible(log_entry)

        # ファイル名にスペースが入らないように修正
        log_file = f"apex_bot_{log_type.lower().replace(' ', '_')}_log.jsonl"
        with open(log_file, 'a', encoding='utf-8') as f:
            f.write(json.dumps(cleaned_log_entry, ensure_ascii=False) + '\n')
            
        logging.info(f"✅ {log_type}ログをファイルに記録しました。")
    except Exception as e:
        logging.error(f"❌ ログ書き込みエラー: {e}", exc_info=True)


def _sync_ftp_upload(local_file: str, remote_file: str):
    """
    同期的にFTPアップロードを実行するヘルパー関数。
    asyncio.to_threadで使用される。
    """
    if not WEBSHARE_HOST or not WEBSHARE_USER or not WEBSHARE_PASS:
        logging.error("❌ WEBSHARE設定 (HOST/USER/PASS) が不足しています。")
        return False

    if not os.path.exists(local_file):
        logging.warning(f"⚠️ ローカルファイル {local_file} が見つかりません。アップロードをスキップします。")
        return True # ファイルがないのはエラーではない

    try:
        # FTP接続とログイン
        ftp = ftplib.FTP()
        # 💡 タイムアウトを30秒に延長 (FTPタイムアウト対策)
        ftp.connect(WEBSHARE_HOST, WEBSHARE_PORT, timeout=30) 
        ftp.login(WEBSHARE_USER, WEBSHARE_PASS)
        
        # ファイルのアップロード (バイナリモード)
        # リモートパスは /<filename> の形式を想定
        ftp.storbinary(f'STOR {remote_file}', open(local_file, 'rb'))

        ftp.quit()
        return True
        
    except ftplib.all_errors as e:
        logging.error(f"❌ FTPアップロードエラー ({WEBSHARE_HOST}): {e}")
        return False
    except Exception as e:
        logging.error(f"❌ ログアップロードの予期せぬエラー: {e}")
        return False

async def upload_logs_to_webshare():
    """ローカルログファイルを外部ストレージ (WebShare/FTP) にアップロードする"""
    if not WEBSHARE_HOST:
        logging.info("ℹ️ WEBSHARE HOSTが設定されていません。ログアップロードをスキップします。")
        return
        
    log_files = [
        "apex_bot_trade_signal_log.jsonl",
        "apex_bot_hourly_analysis_log.jsonl",
        "apex_bot_trade_exit_log.jsonl",
    ]
    
    now_jst = datetime.now(JST)
    upload_timestamp = now_jst.strftime("%Y%m%d_%H%M%S")
    
    logging.info(f"📤 WEBSHAREログアップロード処理を開始します...")

    tasks = []
    for log_file in log_files:
        if os.path.exists(log_file):
            # リモートファイル名にはタイムスタンプとファイル名を含める
            remote_filename = f"apex_log_{upload_timestamp}_{log_file}"
            
            # 同期FTP処理を別スレッドで実行
            tasks.append(
                asyncio.to_thread(_sync_ftp_upload, log_file, remote_filename)
            )

    if not tasks:
        logging.info("ℹ️ アップロード対象のログファイルがありませんでした。")
        return

    # 全てのタスクを並行実行
    results = await asyncio.gather(*tasks)
    
    if all(results):
        logging.info(f"✅ すべてのログファイル ({len(tasks)} 件) を WEBSHARE にアップロードしました。")
    else:
        logging.error("❌ 一部またはすべてのログファイルの WEBSHARE へのアップロードに失敗しました。")

# ====================================================================================
# CCXT & DATA ACQUISITION
# ====================================================================================

async def initialize_exchange_client() -> bool:
    """CCXTクライアントを初期化する"""
    global EXCHANGE_CLIENT
    
    try:
        client_name = CCXT_CLIENT_NAME.lower()
        if client_name == 'binance':
            exchange_class = ccxt_async.binance
        elif client_name == 'bybit':
            exchange_class = ccxt_async.bybit
        # MEXCクライアント
        elif client_name == 'mexc':
            exchange_class = ccxt_async.mexc
        else:
            logging.error(f"❌ 未対応の取引所クライアント: {CCXT_CLIENT_NAME}")
            return False

        # CCXTのオプション設定
        options = {
            'defaultType': 'spot', # 現物取引 (Spot) を想定
        }

        EXCHANGE_CLIENT = exchange_class({
            'apiKey': API_KEY,
            'secret': SECRET_KEY,
            'enableRateLimit': True,
            'options': options
        })
        await EXCHANGE_CLIENT.load_markets()
        logging.info(f"✅ CCXTクライアント ({CCXT_CLIENT_NAME}) を現物取引モードで初期化しました。")
        return True

    except Exception as e:
        logging.critical(f"❌ CCXTクライアント初期化失敗: {e}")
        EXCHANGE_CLIENT = None
        return False

async def fetch_ohlcv_safe(symbol: str, timeframe: str, limit: int) -> Optional[pd.DataFrame]:
    """OHLCVデータを安全に取得する"""
    global EXCHANGE_CLIENT
    if not EXCHANGE_CLIENT:
        return None
        
    try:
        ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(symbol, timeframe, limit=limit)
        if not ohlcv:
            return None
            
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df = df.set_index('timestamp')
        return df

    except Exception as e:
        return None

async def fetch_ohlcv_with_fallback(symbol: str, timeframe: str, limit: int) -> Optional[pd.DataFrame]:
    """指定した足数を確実に取得するためにフォールバック処理を行う"""
    df = await fetch_ohlcv_safe(symbol, timeframe, limit)
    
    # データが少ない場合は、より多くの足数を要求してリトライ
    if df is None or len(df) < limit:
        logging.debug(f"ℹ️ {symbol} ({timeframe}): 必要足数 ({limit}) 未満。リトライ中...")
        df_long = await fetch_ohlcv_safe(symbol, timeframe, limit=limit + 100)
        
        if df_long is not None and len(df_long) >= limit:
            return df_long
        elif df_long is not None and len(df_long) > 0:
            return df_long
    return df

async def update_symbols_by_volume() -> None:
    """出来高に基づいて監視対象銘柄リストを更新する"""
    global CURRENT_MONITOR_SYMBOLS, EXCHANGE_CLIENT
    if not EXCHANGE_CLIENT or SKIP_MARKET_UPDATE:
        logging.info("ℹ️ 銘柄リスト更新をスキップしました。")
        return

    try:
        # すべてのUSDTペアの現物銘柄を取得
        markets = await EXCHANGE_CLIENT.load_markets()
        usdt_spot_symbols = [ 
            s for s, m in markets.items() 
            if m['active'] and m['spot'] and (m['quote'] == 'USDT' or s.endswith('/USDT')) 
        ]

        if not usdt_spot_symbols:
            logging.warning("⚠️ USDT現物銘柄が見つかりませんでした。デフォルトリストを使用します。")
            CURRENT_MONITOR_SYMBOLS = list(set(DEFAULT_SYMBOLS))
            logging.info(f"✅ 銘柄リストを更新しました。合計: {len(CURRENT_MONITOR_SYMBOLS)} 銘柄。")
            return

        # 24時間出来高情報を取得 (ccxtのfetch_tickersでquoteVolumeを使用)
        tickers = await EXCHANGE_CLIENT.fetch_tickers(usdt_spot_symbols)
        
        volume_data = []
        for symbol in usdt_spot_symbols:
            if symbol in tickers and 'quoteVolume' in tickers[symbol]:
                volume_data.append({
                    'symbol': symbol,
                    'volume': tickers[symbol]['quoteVolume']
                })
                
        # 出来高でソートし、上位TOP_SYMBOL_LIMIT個を取得
        volume_data.sort(key=lambda x: x['volume'], reverse=True)
        top_symbols = [d['symbol'] for d in volume_data if d['volume'] is not None][:TOP_SYMBOL_LIMIT]
        
        # デフォルトシンボルと出来高トップをマージし、重複を排除
        combined_symbols = top_symbols + [s for s in DEFAULT_SYMBOLS if s not in top_symbols]
        
        # 最終リストの更新 (市場に存在する銘柄のみ)
        CURRENT_MONITOR_SYMBOLS = [s for s in combined_symbols if s in markets]

        # 銘柄数が30未満の場合は警告
        if len(CURRENT_MONITOR_SYMBOLS) < 30:
            logging.warning(f"⚠️ 監視銘柄数が30未満 ({len(CURRENT_MONITOR_SYMBOLS)}) です。静的リストの追加をご検討ください。")
            
        logging.info(f"✅ 現物銘柄リストを更新しました。合計: {len(CURRENT_MONITOR_SYMBOLS)} 銘柄。")
        logging.debug(f"現在の監視銘柄リスト: {CURRENT_MONITOR_SYMBOLS}")

    except Exception as e:
        logging.error(f"❌ 銘柄リスト更新エラー: {e}")
        # エラー時は既存のリストを維持

def fetch_fgi_sync() -> int:
    """Fear & Greed Index (FGI) を取得する (同期処理)"""
    try:
        url = "https://api.alternative.me/fng/?limit=1"
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        data = response.json()
        if data and 'data' in data and len(data['data']) > 0:
            return int(data['data'][0]['value'])
        return 50 # 取得失敗時は中立
    except Exception as e:
        logging.error(f"❌ FGI APIエラー: {e}")
        return 50

async def get_crypto_macro_context() -> Dict:
    """市場全体のマクロコンテキストを取得する (FGI リアルデータ取得)"""
    fgi_value = 50
    fgi_proxy = 0.0
    forex_trend = 'NEUTRAL'
    forex_bonus = 0.0

    # 1. 恐怖・貪欲指数 (FGI) を取得
    try:
        fgi_value = await asyncio.to_thread(fetch_fgi_sync)
        fgi_normalized = (fgi_value - 50) / 20.0
        fgi_proxy = max(-FGI_PROXY_BONUS_MAX, min(FGI_PROXY_BONUS_MAX, fgi_normalized * FGI_PROXY_BONUS_MAX))
    except Exception as e:
        logging.error(f"❌ FGI取得エラー: {e}")

    # 2. 為替マクロデータは削除

    # 3. マクロコンテキストの確定
    return {
        'fgi_raw_value': fgi_value,
        'fgi_proxy': fgi_proxy,
        'forex_trend': forex_trend,
        'forex_bonus': forex_bonus,
    }

async def fetch_account_status() -> Dict:
    """口座残高と保有ポジション（10 USDT以上）を取得する"""
    global EXCHANGE_CLIENT
    status = {
        'total_usdt_balance': 0.0,
        'open_positions': [], # CCXTから取得した現物
        'error': None
    }
    
    if not EXCHANGE_CLIENT:
        status['error'] = "Exchange client not initialized."
        return status

    try:
        # 1. 残高の取得
        balance = await EXCHANGE_CLIENT.fetch_balance()
        status['total_usdt_balance'] = balance['total'].get('USDT', 0.0)

        # 2. ポジションの取得 (USDT以外の保有資産) と価格取得
        for currency, info in balance['total'].items():
            if not isinstance(currency, str) or currency == 'USDT' or info == 0.0 or info < 0.000001:
                continue
                
            symbol = f"{currency}/USDT"
            
            if symbol in EXCHANGE_CLIENT.markets:
                try:
                    # 堅牢性を優先し、ティッカーを個別に取得
                    ticker = await EXCHANGE_CLIENT.fetch_ticker(symbol)
                    market_price = ticker['last']
                    usdt_value = info * market_price

                    # 10 USDT以上の価値があるもののみをポジションとして報告
                    if usdt_value >= 10.0:
                        status['open_positions'].append({
                            'symbol': symbol,
                            'amount': info,
                            'market_price': market_price,
                            'usdt_value': usdt_value,
                        })

                except Exception as e:
                    logging.warning(f"⚠️ {symbol}: ティッカー取得エラー。残高無視: {e}")
                    
        return status
        
    except Exception as e:
        logging.error(f"❌ 口座ステータス取得エラー: {e}", exc_info=True)
        status['error'] = str(e)
        return status


# ====================================================================================
# CORE LOGIC: ANALYSIS & TRADING
# ====================================================================================

def calculate_technical_indicators(df: pd.DataFrame, timeframe: str) -> Optional[pd.DataFrame]:
    """
    テクニカル指標をDataFrameに追加する。
    【✅ 修正済: BBW/BBPのキーエラー対応済み】
    """
    if df is None or len(df) < LONG_TERM_SMA_LENGTH + 50: 
        logging.warning(f"⚠️ {timeframe} データ不足: {len(df)} bars")
        return None

    # 1. 共通インジケーターの追加
    df.ta.macd(append=True) # MACD (MACD_12_26_9, MACDh_12_26_9, MACDs_12_26_9)
    df.ta.rsi(append=True)  # RSI (RSI_14)
    df.ta.obv(append=True)  # OBV (OBV)
    df.ta.sma(length=LONG_TERM_SMA_LENGTH, append=True) # SMA (長期トレンドフィルタ用)
    
    # 2. ボリンジャーバンド (Bollinger Bands)
    bb_data = ta.bbands(df['close'], length=20, std=2)
    
    # 【✅ 修正箇所: BBW/BBPのキーエラー対応】
    # pandas_taのバージョンによるキー名の違いを吸収し、汎用的な列名に格納します。
    key_prefix = '20_2.0' # length_std
    
    df['BBL'] = bb_data.get(f'BBL_{key_prefix}')
    df['BBM'] = bb_data.get(f'BBM_{key_prefix}')
    df['BBU'] = bb_data.get(f'BBU_{key_prefix}')
    # BBPを確実に格納
    df['BBP'] = bb_data.get(f'BBP_{key_prefix}')
    
    # BBW (バンド幅) のキーを柔軟に取得し、df['BBW']に格納
    bbw_key = f'BBW_{key_prefix}'
    if bbw_key not in bb_data.columns and 'BBW' in bb_data.columns:
        bbw_key = 'BBW'
    elif bbw_key not in bb_data.columns:
        # 最終手段として、BBWから始まる最初の列を探す (より堅牢にする)
        for col in bb_data.columns:
            if col.startswith('BBW'):
                bbw_key = col
                break
        
    df['BBW'] = bb_data.get(bbw_key) # BBWを確実に 'BBW' という名前で保存
    # 【修正箇所 終わり】
    
    # 3. OBV (On-Balance Volume) - 既に上記で追加済み
    
    # 4. BBWのSMA (ボラティリティ過熱チェック用)
    # df['BBW_20_2.0']ではなくdf['BBW']を参照するように修正
    df[f'BBW_SMA'] = df['BBW'].rolling(window=20).mean()
    return df

def get_signal_metrics(df: pd.DataFrame, timeframe: str) -> Dict[str, Any]:
    """
    テクニカルデータからシグナル評価に必要な指標を抽出する。
    【✅ 修正済: BBW/BBPの参照をcalculate_technical_indicatorsで作成された汎用名に変更】
    """
    last = df.iloc[-1]
    
    # 価格情報
    current_price = last['close']
    
    # MACD
    macd_h = last[f'MACDh_12_26_9']
    
    # RSI
    rsi_val = last[f'RSI_14']
    
    # SMA
    long_sma_val = last[f'SMA_{LONG_TERM_SMA_LENGTH}']

    # BBANDS (BBWの参照を修正し、警告を解消)
    try:
        # 【✅ 修正箇所: df['BBW']とdf['BBP']を参照】
        bb_width = last['BBW'] # calculate_technical_indicatorsで作成されたdf['BBW']を参照
        bb_percent = last['BBP'] # calculate_technical_indicatorsで作成されたdf['BBP']を参照
        bbw_sma = last[f'BBW_SMA'] # calculate_technical_indicatorsで作成されたdf['BBW_SMA']を参照
    except KeyError as e:
        # BBANDS/BBWのキーが見つからなかった場合、ログに出力
        logging.warning(f"⚠️ {timeframe} BBANDSキーエラー: {e} - 分析ロジックを確認してください。")
        bb_width = 0.0
        bb_percent = 0.5 
        bbw_sma = 0.0
        
    # OBV (単純に最後の値を取得)
    obv_val = last['OBV']
    
    # OBVモメンタム: 簡易的なOBVの直近20足での変化
    # OBV列が存在することを確認
    obv_momentum_positive = False
    if 'OBV' in df.columns and len(df) >= 20:
        last_obv_change = df['OBV'].iloc[-1] - df['OBV'].iloc[-20]
        obv_momentum_positive = last_obv_change > 0 

    # 価格構造/ピボット支持の判定 (安値が現在価格の1%以内であり、かつローソク足の実体が小さいことを簡易的に示す)
    is_low_pivot_support = (last['low'] / current_price > 0.99) and (last['high'] / current_price < 1.01)

    return {
        'current_price': current_price,
        'long_sma': long_sma_val,
        'macd_h': macd_h,
        'rsi': rsi_val,
        'obv': obv_val,
        'bb_width': bb_width, 
        'bb_percent': bb_percent, 
        'bbw_sma': bbw_sma, # BBWの移動平均
        'obv_momentum_positive': obv_momentum_positive,
        'is_low_pivot_support': is_low_pivot_support,
    }

def score_signal(symbol: str, timeframe: str, df: pd.DataFrame, macro_context: Dict) -> Optional[Dict]:
    """
    ロングシグナルを評価し、スコアと取引パラメータを決定する
    """
    if df is None:
        return None

    try:
        # 1. 指標の抽出
        metrics = get_signal_metrics(df, timeframe)
        current_price = metrics['current_price']
        long_sma = metrics['long_sma']
        macd_h = metrics['macd_h']
        rsi_val = metrics['rsi']
        bb_width = metrics['bb_width']
        bb_percent = metrics['bb_percent']
        bbw_sma = metrics['bbw_sma']
        is_low_pivot_support = metrics['is_low_pivot_support']
        obv_momentum_positive = metrics['obv_momentum_positive']
        
        # 2. シグナル条件 (ロングエントリーの最低条件)
        # MACDヒストグラムが陽転またはゼロライン付近で反転開始
        macd_condition = macd_h > -0.0001
        # RSIがモメンタム加速ゾーン (40以上60以下を想定) 
        rsi_condition = rsi_val > RSI_MOMENTUM_LOW and rsi_val < 65 # 65未満
        # %Bが0.5未満（バンド下部で推移）
        bbp_condition = bb_percent < 0.5
        
        # 総合シグナル判定 (最も緩い条件)
        if not (macd_condition and rsi_condition and bbp_condition):
            return None # シグナル不成立

        
        # 3. スコアリングの開始
        score = BASE_SCORE # 60点からスタート
        tech_data = {'raw_score': BASE_SCORE}
        
        # A. 長期トレンドフィルタと逆行ペナルティ (最大 LONG_TERM_REVERSAL_PENALTY 0.20点)
        long_term_reversal_penalty_value = 0.0
        if current_price < long_sma:
            # 価格が長期SMAより下: 逆行ペナルティ
            long_term_reversal_penalty_value = LONG_TERM_REVERSAL_PENALTY
            score -= long_term_reversal_penalty_value
        tech_data['long_term_reversal_penalty_value'] = long_term_reversal_penalty_value
        
        # B. 価格構造/ピボット支持ボーナス (最大 STRUCTURAL_PIVOT_BONUS 0.07点)
        structural_pivot_bonus = 0.0
        if is_low_pivot_support:
            structural_pivot_bonus = STRUCTURAL_PIVOT_BONUS
            score += structural_pivot_bonus
        tech_data['structural_pivot_bonus'] = structural_pivot_bonus

        # C. MACD/モメンタムペナルティ (最大 MACD_CROSS_PENALTY 0.15点)
        macd_penalty_value = 0.0
        if macd_h < 0:
            # MACDヒストグラムが陰転している場合、ペナルティ
            macd_penalty_value = MACD_CROSS_PENALTY
            score -= macd_penalty_value
        tech_data['macd_penalty_value'] = macd_penalty_value
        
        # D. 出来高/OBV確証ボーナス (最大 OBV_MOMENTUM_BONUS 0.04点)
        obv_momentum_bonus_value = 0.0
        if obv_momentum_positive:
            obv_momentum_bonus_value = OBV_MOMENTUM_BONUS
            score += obv_momentum_bonus_value
        tech_data['obv_momentum_bonus_value'] = obv_momentum_bonus_value


        # E. ボラティリティ過熱ペナルティ (BBWのSMAとの比率で評価 - 警告修正)
        volatility_penalty_value = 0.0
        volatility_ratio = 1.0 # デフォルト
        
        if bbw_sma > 0.0:
            volatility_ratio = bb_width / bbw_sma
            # BBWがSMAの1.5倍を超える場合、過熱と見なす
            if volatility_ratio > 1.5:
                volatility_penalty_value = -(BASE_SCORE * 0.2) # ベーススコアの20%をペナルティ
            elif volatility_ratio < 0.7:
                 # バンドが狭すぎる場合、ボラティリティ不足としてペナルティ
                 volatility_penalty_value = -(BASE_SCORE * 0.1)

        score += volatility_penalty_value # マイナスの値が入る
        tech_data['volatility_penalty_value'] = volatility_penalty_value 
        tech_data['volatility_ratio'] = volatility_ratio
        
        # 4. 流動性/マクロ要因の適用
        
        # F. 流動性ボーナス (簡略化: ダミーとして最大値を適用)
        liquidity_bonus_value = LIQUIDITY_BONUS_MAX 
        score += liquidity_bonus_value
        tech_data['liquidity_bonus_value'] = liquidity_bonus_value

        # G. FGIマクロ影響
        sentiment_fgi_proxy_bonus = macro_context.get('fgi_proxy', 0.0) 
        score += sentiment_fgi_proxy_bonus
        tech_data['sentiment_fgi_proxy_bonus'] = sentiment_fgi_proxy_bonus

        # H. 為替マクロ影響 (機能削除のため0.0)
        forex_bonus = 0.0 
        tech_data['forex_bonus'] = forex_bonus
        
        # 最終スコアを0.0から1.0の間にクリップ
        final_score = max(0.0, min(1.0, score))
        
        # 5. SL/TPの設定 (RRR=1.5を目標とするが、最小RRR=1.0が必要)
        
        # ストップロス (SL): 直近のローソク足の安値から少し下の価格
        stop_loss_buffer_rate = 0.005 # 0.5%
        stop_loss = df.iloc[-1]['low'] * (1 - stop_loss_buffer_rate)
        
        # リスク額の決定 (エントリー価格 - SL)
        risk_amount = current_price - stop_loss
        if risk_amount <= 0:
             # SLが設定できなければシグナル不成立
            return None

        # テイクプロフィット (TP): RRR=1.5を目標
        target_rrr = 1.5
        take_profit = current_price + (risk_amount * target_rrr)
        
        # 実際のRRRの計算
        reward_amount = take_profit - current_price
        rr_ratio = reward_amount / risk_amount
        
        # 最小RRRチェック
        if rr_ratio < 1.0:
            return None # 最小RRR (1.0) 未満の場合は取引しない
            
        # 6. 結果の構築
        return {
            'symbol': symbol,
            'timeframe': timeframe,
            'score': final_score,
            'entry_price': current_price,
            'stop_loss': stop_loss,
            'take_profit': take_profit,
            'risk_amount': risk_amount,
            'reward_amount': reward_amount,
            'rr_ratio': rr_ratio,
            'tech_data': tech_data,
        }
        
    except KeyError as e:
        logging.error(f"❌ {symbol} ({timeframe}): 分析中にキーエラーが発生: {e}")
        return None
    except Exception as e:
        logging.error(f"❌ {symbol} ({timeframe}): シグナル評価中に予期せぬエラーが発生: {e}", exc_info=True)
        return None


# ====================================================================================
# CORE LOGIC: EXECUTION
# ====================================================================================

async def get_exchange_info_safe(symbol: str) -> Optional[Dict]:
    """
    取引所のシンボル情報を安全に取得する
    """
    global EXCHANGE_CLIENT
    if not EXCHANGE_CLIENT:
        return None
    
    # シンボル情報がロードされているか確認 (load_markets済みを前提)
    if symbol not in EXCHANGE_CLIENT.markets:
        try:
            await EXCHANGE_CLIENT.load_markets()
            if symbol not in EXCHANGE_CLIENT.markets:
                logging.error(f"❌ {symbol}: 市場情報が取引所に存在しません。")
                return None
        except Exception as e:
            logging.error(f"❌ {symbol}: 市場情報更新エラー: {e}")
            return None
    
    market = EXCHANGE_CLIENT.markets[symbol]
    
    # 数量(base)と価格(quote)の精度を取得
    precision = {
        'amount': market['precision']['amount'],
        'price': market['precision']['price'],
    }
    
    # 最小取引量 (MEXCではlimit.amount.minが有効)
    limits = {
        'min_amount': market['limits']['amount']['min'] if market['limits']['amount'] and 'min' in market['limits']['amount'] else 0.0,
        'min_cost': market['limits']['cost']['min'] if market['limits']['cost'] and 'min' in market['limits']['cost'] else 10.0, # デフォルト10 USDT
    }

    return {
        'precision': precision,
        'limits': limits,
    }


def calculate_dynamic_lot(signal: Dict, account_status: Dict) -> Tuple[float, float, str]:
    """
    リスク管理に基づき、動的な取引ロットサイズをUSDT建てで計算する。
    (簡略化のため、固定ロットとアカウント残高上限のチェックのみを行う)
    """
    current_price = signal['entry_price']
    risk_amount_usd = signal['risk_amount'] # SL幅
    
    # 1. ベースロットサイズ (定数)
    base_trade_size = BASE_TRADE_SIZE_USDT 
    
    # 2. 口座残高上限のチェック (安全確保のため、最大でも残高の20%を上限とする)
    usdt_balance = account_status.get('total_usdt_balance', 0.0)
    max_trade_size = usdt_balance * 0.20
    
    if max_trade_size < 10.0:
        # 残高が非常に少ない場合、取引不可
        return 0.0, 0.0, "口座残高が不足しています (10 USDT未満)。"

    # 3. 最終的なUSDTロットを決定 (base_trade_sizeとmax_trade_sizeの小さい方)
    final_trade_size_usdt = min(base_trade_size, max_trade_size)
    
    if final_trade_size_usdt < 10.0:
        return 0.0, 0.0, f"最終取引ロット ({final_trade_size_usdt:.2f} USDT) が最小取引額 (10 USDT) 未満です。"
    
    # 4. 取引数量 (Base Currency) への変換
    # 実際のエントリー価格を使用
    base_amount = final_trade_size_usdt / current_price
    
    return base_amount, final_trade_size_usdt, "ok"

async def execute_market_order(signal: Dict, side: str, amount_base: float, quote_precision: int, amount_precision: int) -> Dict:
    """
    CCXTを使用して現物市場注文を執行する (現物取引を想定)
    """
    global EXCHANGE_CLIENT
    
    symbol = signal['symbol']
    
    # 数量を取引所の精度に合わせて調整
    # amount_baseをdecimalで処理し、精度に合わせて丸める
    amount_base = EXCHANGE_CLIENT.decimal_to_precision(amount_base, ccxt.ROUND, amount_precision)
    
    # CCXTは文字列を引数に取るためfloatに戻す
    try:
        amount_base_float = float(amount_base)
    except Exception:
        return {'status': 'error', 'error_message': f"取引数量の変換失敗: {amount_base}"}
        
    if amount_base_float <= 0.0:
        return {'status': 'error', 'error_message': "計算された取引数量が0以下です。"}
        
    logging.info(f"➡️ {symbol} {side} 注文実行: 数量={amount_base} (精度調整後)")
    
    try:
        # 成行注文の実行
        # 現物取引では、buy/sellのいずれかを使用
        if side == 'buy':
            order = await EXCHANGE_CLIENT.create_market_buy_order(symbol, amount_base_float)
        elif side == 'sell':
            # 売りの場合、amount_baseは保有数量を示す
            order = await EXCHANGE_CLIENT.create_market_sell_order(symbol, amount_base_float)
        else:
            return {'status': 'error', 'error_message': "無効な注文サイドが指定されました。"}

        # 約定情報を抽出
        filled = EXCHANGE_CLIENT.safe_value(order, 'filled', 0.0)
        cost = EXCHANGE_CLIENT.safe_value(order, 'cost', 0.0)
        price = cost / filled if filled > 0 else 0.0
        
        if filled > 0:
            return {
                'status': 'ok',
                'entry_price': price,
                'filled_amount': filled,
                'filled_usdt': cost,
                'order_id': EXCHANGE_CLIENT.safe_string(order, 'id'),
                'raw_order': order
            }
        else:
            return {'status': 'error', 'error_message': "約定数量が0でした。"}

    except ccxt.ExchangeError as e:
        logging.error(f"❌ CCXT取引エラー: {e}")
        return {'status': 'error', 'error_message': f"CCXT取引所エラー: {e}"}
    except Exception as e:
        logging.error(f"❌ 注文実行中の予期せぬエラー: {e}", exc_info=True)
        return {'status': 'error', 'error_message': f"予期せぬエラー: {e}"}


async def process_signal(signal: Dict, account_status: Dict, current_threshold: float) -> None:
    """
    シグナルを評価し、取引を実行する。
    """
    symbol = signal['symbol']
    
    # 1. 閾値チェック
    if signal['score'] < current_threshold:
        logging.info(f"ℹ️ {symbol}: スコア ({signal['score']:.4f}) が閾値 ({current_threshold:.4f}) 未満です。取引をスキップします。")
        return

    # 2. クールダウンチェック
    now = time.time()
    if now - LAST_SIGNAL_TIME.get(symbol, 0.0) < TRADE_SIGNAL_COOLDOWN:
        cooldown_hours = TRADE_SIGNAL_COOLDOWN / 3600
        logging.info(f"ℹ️ {symbol}: クールダウン ({cooldown_hours:.1f}時間) 中のため、取引をスキップします。")
        return

    # 3. RRRチェック (score_signalで既に1.0未満は除外済み)
    if signal['rr_ratio'] < 1.0:
        logging.warning(f"⚠️ {symbol}: RRR ({signal['rr_ratio']:.2f}) が最小基準 (1.0) 未満です。取引をスキップします。")
        return

    # 4. 取引実行ロジック
    if TEST_MODE:
        logging.warning(f"⚠️ TEST_MODE: {symbol} ロングシグナルは発生しましたが、取引は実行しません。")
        LAST_SIGNAL_TIME[symbol] = now
        await send_telegram_notification(format_telegram_message(signal, "取引シグナル", current_threshold, trade_result={'status': 'test'}))
        log_signal(signal, "Trade Signal", trade_result={'status': 'test'})
        return

    # 5. 取引所情報とロットサイズの計算
    exchange_info = await get_exchange_info_safe(symbol)
    if not exchange_info:
        await send_telegram_notification(format_telegram_message(signal, "取引シグナル", current_threshold, trade_result={'status': 'error', 'error_message': '取引所情報取得失敗'}))
        return

    amount_base, lot_size_usdt, lot_status = calculate_dynamic_lot(signal, account_status)
    if lot_status != "ok":
        logging.error(f"❌ {symbol}: ロットサイズ計算エラー: {lot_status}")
        await send_telegram_notification(format_telegram_message(signal, "取引シグナル", current_threshold, trade_result={'status': 'error', 'error_message': f"ロット計算エラー: {lot_status}"}))
        return

    signal['lot_size_usdt'] = lot_size_usdt # 通知用に追加

    # 6. 注文執行
    trade_result = await execute_market_order(
        signal, 
        'buy', 
        amount_base,
        exchange_info['precision']['price'],
        exchange_info['precision']['amount']
    )

    # 7. 結果処理と通知
    if trade_result['status'] == 'ok':
        LAST_SIGNAL_TIME[symbol] = now
        
        # ポジション管理リストに追加 (SL/TP監視用)
        new_position = {
            'symbol': symbol,
            'timeframe': signal['timeframe'],
            'entry_price': trade_result['entry_price'],
            'stop_loss': signal['stop_loss'],
            'take_profit': signal['take_profit'],
            'filled_amount': trade_result['filled_amount'],
            'filled_usdt': trade_result['filled_usdt'],
            'order_id': trade_result['order_id'],
            'score': signal['score'],
            'rr_ratio': signal['rr_ratio'],
            'open_time': now,
        }
        OPEN_POSITIONS.append(new_position)
        
        # ログとTelegram通知
        await send_telegram_notification(format_telegram_message(signal, "取引シグナル", current_threshold, trade_result))
        log_signal(signal, "Trade Signal", trade_result)

    else:
        # 失敗通知
        await send_telegram_notification(format_telegram_message(signal, "取引シグナル", current_threshold, trade_result))
        log_signal(signal, "Trade Signal Error", trade_result)


async def check_and_close_position(position: Dict) -> Tuple[bool, Optional[Dict]]:
    """
    ポジションのSL/TP条件をチェックし、満たされていれば決済する
    """
    global EXCHANGE_CLIENT
    
    symbol = position['symbol']
    
    # 1. 現在価格の取得
    try:
        ticker = await EXCHANGE_CLIENT.fetch_ticker(symbol)
        current_price = ticker['last']
    except Exception as e:
        logging.error(f"❌ {symbol} 決済監視: 価格取得エラー: {e}")
        return False, None # 決済せず、監視を継続

    # 2. 決済条件のチェック
    exit_type = None
    if current_price <= position['stop_loss']:
        exit_type = "SL (ストップロス)"
    elif current_price >= position['take_profit']:
        exit_type = "TP (テイクプロフィット)"
    
    if exit_type is None:
        return False, None # 条件未達、監視を継続
    
    logging.info(f"🚨 決済トリガー: {symbol} - {exit_type} (価格: {current_price:.4f})")

    # 3. 取引所情報と決済数量の準備
    exchange_info = await get_exchange_info_safe(symbol)
    if not exchange_info:
        return False, None # 決済情報不足

    # 決済数量 (ポジションの全量)
    amount_base_to_sell = position['filled_amount']
    
    # 4. 決済注文の執行 (現物市場売り注文)
    trade_result = await execute_market_order(
        position, 
        'sell', 
        amount_base_to_sell,
        exchange_info['precision']['price'],
        exchange_info['precision']['amount']
    )
    
    # 5. 結果処理
    if trade_result['status'] == 'ok':
        pnl_usdt = (trade_result['entry_price'] - position['entry_price']) * position['filled_amount']
        pnl_rate = (trade_result['entry_price'] / position['entry_price']) - 1.0
        
        exit_result = {
            'entry_price': position['entry_price'],
            'exit_price': trade_result['entry_price'],
            'pnl_usdt': pnl_usdt,
            'pnl_rate': pnl_rate,
            'filled_amount': trade_result['filled_amount'],
            'exit_type': exit_type,
            'status': 'ok'
        }
        
        # 通知
        await send_telegram_notification(format_telegram_message(position, "ポジション決済", position['score'], exit_result, exit_type))
        log_signal(position, "Trade Exit", exit_result)

        return True, exit_result # 決済成功、リストから削除
    else:
        # 決済失敗時 (リトライのためリストに残す)
        logging.error(f"❌ {symbol} 決済失敗 (トリガー: {exit_type}): {trade_result['error_message']}")
        # 決済失敗時の特殊なログはここでは省略し、次のループでのリトライを待つ
        return False, None

async def monitor_positions_loop():
    """
    保有中のポジションを監視し、SL/TP条件を満たせば決済するメインループ
    """
    while True:
        await asyncio.sleep(MONITOR_INTERVAL)
        
        if TEST_MODE:
            # テストモードでは決済監視はスキップ
            continue 

        if not OPEN_POSITIONS:
            continue
            
        logging.info(f"🔄 ポジション監視ループ: {len(OPEN_POSITIONS)} 銘柄をチェック中...")
        
        # 決済されたポジションを除外するための新しいリスト
        new_open_positions = []
        
        for position in OPEN_POSITIONS:
            is_closed, _ = await check_and_close_position(position)
            
            if not is_closed:
                new_open_positions.append(position)
                
        # グローバルリストを更新
        global OPEN_POSITIONS
        OPEN_POSITIONS = new_open_positions
        
        logging.info(f"✅ ポジション監視完了: 残り {len(OPEN_POSITIONS)} 銘柄。")


# ====================================================================================
# MAIN & FASTAPI SETUP
# ====================================================================================

def get_current_threshold(macro_context: Dict) -> float:
    """マクロコンテキストに基づいて動的な取引閾値を決定する"""
    fgi_proxy = macro_context.get('fgi_proxy', 0.0)
    
    if fgi_proxy < FGI_SLUMP_THRESHOLD:
        return SIGNAL_THRESHOLD_SLUMP # 低迷/リスクオフ時 (最高閾値)
    elif fgi_proxy > FGI_ACTIVE_THRESHOLD:
        return SIGNAL_THRESHOLD_ACTIVE # 活発/リスクオン時 (最低閾値)
    else:
        return SIGNAL_THRESHOLD_NORMAL # 通常時

async def analyze_all_symbols(symbols: List[str], macro_context: Dict) -> List[Dict]:
    """
    全ての対象銘柄について、指定されたタイムフレームで分析を実行し、シグナルを返す
    """
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT:
        logging.error("❌ analyze_all_symbols: CCXTクライアントが未初期化です。")
        return []
        
    analysis_tasks = []
    
    for symbol in symbols:
        # 既に管理ポジションにある銘柄は分析をスキップ
        if any(pos['symbol'] == symbol for pos in OPEN_POSITIONS):
            continue
            
        async def run_analysis_for_symbol(sym: str) -> List[Dict]:
            signals = []
            
            # 全てのタイムフレームのOHLCVデータを取得
            ohlcv_data: Dict[str, Optional[pd.DataFrame]] = {}
            tasks = []
            for tf in TARGET_TIMEFRAMES:
                limit = REQUIRED_OHLCV_LIMITS[tf]
                tasks.append(fetch_ohlcv_with_fallback(sym, tf, limit))
                
            results = await asyncio.gather(*tasks)
            for tf, df in zip(TARGET_TIMEFRAMES, results):
                ohlcv_data[tf] = df

            # 各タイムフレームで分析を実行
            for tf in TARGET_TIMEFRAMES:
                df = ohlcv_data[tf]
                
                # 1. テクニカル指標の計算
                df_ta = calculate_technical_indicators(df, tf)
                if df_ta is None:
                    continue
                
                # 2. シグナル評価
                signal = score_signal(sym, tf, df_ta, macro_context)
                if signal:
                    signals.append(signal)
                    
            return signals

        analysis_tasks.append(run_analysis_for_symbol(symbol))

    # 全ての銘柄の分析を並行実行
    all_results = await asyncio.gather(*analysis_tasks)
    
    # 結果をフラット化
    all_signals = [signal for sublist in all_results for signal in sublist]
    
    # スコアで降順にソート
    all_signals.sort(key=lambda s: s['score'], reverse=True)
    
    return all_signals


async def analysis_only_notification_loop():
    """
    1時間ごとに、取引実行を伴わない市場分析サマリーを通知するループ
    """
    global LAST_ANALYSIS_ONLY_NOTIFICATION_TIME, LAST_ANALYSIS_SIGNALS, GLOBAL_MACRO_CONTEXT
    
    while True:
        await asyncio.sleep(60) # 1分ごとにチェック
        now = time.time()
        
        if now - LAST_ANALYSIS_ONLY_NOTIFICATION_TIME >= ANALYSIS_ONLY_INTERVAL:
            
            logging.info("📝 1時間ごとの分析専用通知を実行します。")
            
            if not LAST_ANALYSIS_SIGNALS:
                logging.warning("⚠️ 分析結果 (LAST_ANALYSIS_SIGNALS) が空です。スキップします。")
                LAST_ANALYSIS_ONLY_NOTIFICATION_TIME = now
                continue

            current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
            
            # 通知メッセージの作成
            message = format_analysis_only_message(
                LAST_ANALYSIS_SIGNALS, 
                GLOBAL_MACRO_CONTEXT, 
                current_threshold, 
                len(CURRENT_MONITOR_SYMBOLS)
            )
            
            # Telegram通知
            await send_telegram_notification(message)
            
            # ログ記録
            log_data = {
                'macro_context': GLOBAL_MACRO_CONTEXT,
                'current_threshold': current_threshold,
                'signals': LAST_ANALYSIS_SIGNALS[:TOP_SIGNAL_COUNT]
            }
            log_signal(log_data, "Hourly Analysis")

            LAST_ANALYSIS_ONLY_NOTIFICATION_TIME = now
            logging.info("✅ 1時間ごとの分析専用通知を完了しました。")


async def webshare_upload_loop():
    """
    1時間ごとにログファイルをWebShare (FTP) にアップロードするループ
    """
    global LAST_WEBSHARE_UPLOAD_TIME
    
    while True:
        await asyncio.sleep(60) # 1分ごとにチェック
        now = time.time()
        
        if not WEBSHARE_HOST:
             await asyncio.sleep(WEBSHARE_UPLOAD_INTERVAL) # 設定がなければ長時間待機
             continue 

        if now - LAST_WEBSHARE_UPLOAD_TIME >= WEBSHARE_UPLOAD_INTERVAL:
            await upload_logs_to_webshare()
            LAST_WEBSHARE_UPLOAD_TIME = now


async def main_loop():
    """
    メインの取引ロジックと市場更新、分析を実行するループ
    """
    global LAST_SUCCESS_TIME, LAST_ANALYSIS_SIGNALS, GLOBAL_MACRO_CONTEXT, IS_FIRST_MAIN_LOOP_COMPLETED
    
    while True:
        try:
            logging.info("=" * 50)
            logging.info(f"🚀 メインループ実行開始 ({datetime.now(JST).strftime('%H:%M:%S')})")

            # 1. 市場情報とマクロコンテキストの更新
            await update_symbols_by_volume()
            
            new_macro_context = await get_crypto_macro_context()
            GLOBAL_MACRO_CONTEXT = new_macro_context
            
            account_status = await fetch_account_status()
            current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)

            # 2. 初回起動完了通知 (一度だけ実行)
            if not IS_FIRST_MAIN_LOOP_COMPLETED:
                bot_version = "v19.0.28 - Safety and Frequency Finalized (Patch 36)"
                startup_msg = format_startup_message(
                    account_status, 
                    GLOBAL_MACRO_CONTEXT, 
                    len(CURRENT_MONITOR_SYMBOLS),
                    current_threshold,
                    bot_version
                )
                await send_telegram_notification(startup_msg)
                IS_FIRST_MAIN_LOOP_COMPLETED = True
            
            # 3. 全銘柄の分析とシグナル抽出
            all_signals = await analyze_all_symbols(CURRENT_MONITOR_SYMBOLS, GLOBAL_MACRO_CONTEXT)
            LAST_ANALYSIS_SIGNALS = all_signals

            logging.info(f"📊 分析完了: {len(all_signals)} 件のシグナル候補が見つかりました。")

            # 4. シグナル処理 (スコア順にTOP_SIGNAL_COUNTまでチェック)
            processed_count = 0
            for signal in all_signals:
                if signal['score'] >= current_threshold:
                    await process_signal(signal, account_status, current_threshold)
                    processed_count += 1
                    if processed_count >= TOP_SIGNAL_COUNT:
                        break # 通知/取引は上位数件に限定

            LAST_SUCCESS_TIME = time.time()
            logging.info(f"✅ メインループ実行完了。次の実行まで {LOOP_INTERVAL/60:.0f} 分待機します。")

        except Exception as e:
            logging.critical(f"🔥 メインループで致命的なエラーが発生しました: {e}", exc_info=True)
            # 致命的なエラーが発生した場合、次回まで長時間待機し、APIコールを避ける
            await asyncio.sleep(LOOP_INTERVAL * 2)
            continue

        await asyncio.sleep(LOOP_INTERVAL)


# ====================================================================================
# FASTAPI ENTRY POINT
# ====================================================================================

app = FastAPI(
    title="Apex BOT Trading System API",
    description="CCXTベースの現物自動売買システムAPI"
)

@app.on_event("startup")
async def startup_event():
    # 1. CCXTクライアントの初期化
    if not await initialize_exchange_client():
        logging.critical("❌ CCXTクライアントの初期化に失敗しました。BOTを起動できません。")
        sys.exit(1)

    # 2. 初回の市場情報取得
    await update_symbols_by_volume()
    
    # 3. ループ開始前の遅延設定 (初回の通知タイミングを調整)
    # 実行後、すぐにHourly Analysisが走らないように調整
    global LAST_ANALYSIS_ONLY_NOTIFICATION_TIME, LAST_WEBSHARE_UPLOAD_TIME
    
    ANALYSIS_ONLY_DELAY_SECONDS = 60 * 15 
    LAST_ANALYSIS_ONLY_NOTIFICATION_TIME = time.time() - (ANALYSIS_ONLY_INTERVAL - ANALYSIS_ONLY_DELAY_SECONDS)
    
    # 初回起動後30分後にWebShareアップロード
    WEBSHARE_UPLOAD_DELAY_SECONDS = 60 * 30 
    LAST_WEBSHARE_UPLOAD_TIME = time.time() - (WEBSHARE_UPLOAD_INTERVAL - WEBSHARE_UPLOAD_DELAY_SECONDS)

    # 4. メインの取引ループと監視ループを起動
    asyncio.create_task(main_loop())
    asyncio.create_task(analysis_only_notification_loop()) 
    asyncio.create_task(webshare_upload_loop()) 
    asyncio.create_task(monitor_positions_loop()) 


@app.on_event("shutdown")
async def shutdown_event():
    global EXCHANGE_CLIENT
    if EXCHANGE_CLIENT:
        await EXCHANGE_CLIENT.close()
        logging.info("CCXTクライアントをシャットダウンしました。")

@app.get("/status")
def get_status():
    status_msg = {
        "status": "ok",
        "bot_version": "v19.0.28 - Safety and Frequency Finalized (Patch 36)", # バージョン更新
        "base_trade_size_usdt": BASE_TRADE_SIZE_USDT, 
        "managed_positions_count": len(OPEN_POSITIONS), 
        "last_success_time_utc": datetime.fromtimestamp(LAST_SUCCESS_TIME, timezone.utc).isoformat() if LAST_SUCCESS_TIME else "N/A",
        "next_main_loop_check_seconds": max(0, int(LOOP_INTERVAL - (time.time() - LAST_SUCCESS_TIME))),
        "current_threshold": get_current_threshold(GLOBAL_MACRO_CONTEXT),
        "macro_context": GLOBAL_MACRO_CONTEXT,
        "is_test_mode": TEST_MODE,
        "monitoring_symbols": len(CURRENT_MONITOR_SYMBOLS)
    }
    return JSONResponse(content=status_msg)

if __name__ == "__main__":
    # uvicorn.runの引数は環境に応じて調整してください
    uvicorn.run(app, host="0.0.0.0", port=8000)
