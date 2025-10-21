# ====================================================================================
#Apex BOT v19.0.28 - Safety and Frequency Finalized (Patch 36)
#
# 修正ポイント:
# 1. 【安全確認】動的取引閾値 (0.70, 0.65, 0.60) を最終確定。
# 2. 【安全確認】取引実行ロジック (SL/TP, RRR >= 1.0, CCXT精度調整) の堅牢性を再確認。
# 3. 【バージョン更新】全てのバージョン情報を Patch 36 に更新。
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
                 # Noneチェックを追加
                if tickers[symbol]['quoteVolume'] is not None:
                    volume_data.append({
                        'symbol': symbol,
                        'volume': tickers[symbol]['quoteVolume']
                    })

        # 出来高でソートし、上位TOP_SYMBOL_LIMIT個を取得
        volume_data.sort(key=lambda x: x['volume'], reverse=True)
        top_symbols = [d['symbol'] for d in volume_data][:TOP_SYMBOL_LIMIT]
        
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
        # FGIを-20から+20の範囲で正規化し、ボーナス最大値にクリップ
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
                            'usdt_value': usdt_value
                        })
                except Exception as e:
                    logging.warning(f"⚠️ {symbol} の価格取得エラー: {e}")


    except Exception as e:
        logging.error(f"❌ 口座ステータス取得エラー: {e}")
        status['error'] = str(e)

    return status


# ====================================================================================
# STRATEGY & SCORING
# ====================================================================================

def calculate_technical_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """テクニカル指標を計算し、DataFrameに追加する"""
    
    if len(df) < LONG_TERM_SMA_LENGTH + 1:
        logging.warning(f"⚠️ データ不足: 長期SMAに必要なデータがありません ({len(df)}/{LONG_TERM_SMA_LENGTH})")
        return df

    # 1. 移動平均線 (SMA)
    df['SMA_200'] = ta.sma(df['close'], length=LONG_TERM_SMA_LENGTH)
    df['SMA_50'] = ta.sma(df['close'], length=50)
    df['SMA_20'] = ta.sma(df['close'], length=20)

    # 2. RSI (Relative Strength Index)
    df['RSI'] = ta.rsi(df['close'], length=14)

    # 3. MACD (Moving Average Convergence Divergence)
    macd_data = ta.macd(df['close'], fast=12, slow=26, signal=9)
    df['MACD'] = macd_data['MACD_12_26_9']
    df['MACDh'] = macd_data['MACDh_12_26_9']
    df['MACDs'] = macd_data['MACDs_12_26_9']
    
    # 4. ボリンジャーバンド (Bollinger Bands)
    bb_data = ta.bbands(df['close'], length=20, std=2)
    df['BBL'] = bb_data['BBL_20_2.0']
    df['BBM'] = bb_data['BBM_20_2.0']
    df['BBU'] = bb_data['BBU_20_2.0']
    df['BBW'] = bb_data['BBW_20_2.0'] # バンド幅
    
    # 5. OBV (On-Balance Volume)
    df['OBV'] = ta.obv(df['close'], df['volume'])
    df['OBV_SMA_10'] = ta.sma(df['OBV'], length=10) # OBVの短期SMA

    # 6. ATR (Average True Range) - TP/SL計算用
    df['ATR'] = ta.atr(df['high'], df['low'], df['close'], length=14)

    # 7. ピボットポイント (Pivot Points - Classic) - 構造分析用
    # 最新の足を省くため、手動で計算
    pivot_data = ta.pivot_points(df['high'], df['low'], df['close'], method="fibonacci") # フィボナッチピボットを使用
    df['PP'] = pivot_data['PP_D']
    df['S1'] = pivot_data['S1_D']
    df['S2'] = pivot_data['S2_D']
    df['R1'] = pivot_data['R1_D']
    df['R2'] = pivot_data['R2_D']
    
    # NaNを削除
    df = df.dropna()
    
    return df

def analyze_and_score_long(df: pd.DataFrame, timeframe: str, macro_context: Dict, liquidity_bonus: float) -> Tuple[float, Dict]:
    """
    ロングシグナルに対するスコアリングと詳細分析を行う。
    スコアは0.0から1.0。ベース60点 + 加点/減点。
    """
    if df.empty or len(df) < 2:
        return 0.0, {}
        
    last = df.iloc[-1]
    prev = df.iloc[-2]
    
    score_details = {}
    current_score = BASE_SCORE # 60点からスタート
    
    # --- 1. 長期トレンドフィルター/構造分析 (最大加点: +0.27 / 最大減点: -0.20) ---
    
    # a. 長期トレンドフィルター (SMA 200) - 逆行でペナルティ -0.20
    is_long_term_bullish = last['close'] > last['SMA_200']
    long_term_reversal_penalty = 0.0
    if not is_long_term_bullish:
        long_term_reversal_penalty = LONG_TERM_REVERSAL_PENALTY # -0.20
    
    current_score -= long_term_reversal_penalty
    score_details['long_term_reversal_penalty_value'] = long_term_reversal_penalty
    score_details['is_long_term_bullish'] = is_long_term_bullish
    
    # b. 価格構造/ピボット支持 (最大ボーナス: +0.07)
    structural_pivot_bonus = 0.0
    # 価格が直近のピボットサポート(S1 or S2)付近で反発しているか、またはSMA 50/20の上にある
    is_supported_by_pivot = (last['close'] > last['S1'] and last['low'] <= last['PP']) or \
                            (last['close'] > last['S2'] and last['low'] <= last['S1']) 
                            
    is_supported_by_sma = last['close'] > last['SMA_50'] and last['close'] > last['SMA_20']

    if is_supported_by_pivot or is_supported_by_sma:
        structural_pivot_bonus = STRUCTURAL_PIVOT_BONUS # +0.07

    current_score += structural_pivot_bonus
    score_details['structural_pivot_bonus'] = structural_pivot_bonus
    
    # --- 2. モメンタム/出来高 (最大加点: +0.04 / 最大減点: -0.15) ---
    
    # a. RSIモメンタム (RSI < 40 でロング優位と見なす)
    is_rsi_long_momentum = last['RSI'] <= RSI_MOMENTUM_LOW # 40以下 
    
    # b. MACDクロス/モメンタムペナルティ -0.15
    macd_penalty = 0.0
    is_macd_bearish_cross = last['MACD'] < last['MACDs'] # デッドクロスまたはクロス以下
    is_macd_divergence_bearish = last['MACDh'] < 0 and prev['MACDh'] < 0 # MACDヒストグラムが2本連続でマイナス (モメンタム低下)

    if is_macd_bearish_cross or is_macd_divergence_bearish:
        macd_penalty = MACD_CROSS_PENALTY # -0.15
    
    current_score -= macd_penalty
    score_details['macd_penalty_value'] = macd_penalty
    score_details['is_rsi_long_momentum'] = is_rsi_long_momentum
    
    # c. 出来高/OBV確証ボーナス +0.04
    obv_momentum_bonus = 0.0
    is_obv_rising = last['OBV'] > last['OBV_SMA_10'] 

    if is_obv_rising:
        obv_momentum_bonus = OBV_MOMENTUM_BONUS # +0.04
        
    current_score += obv_momentum_bonus
    score_details['obv_momentum_bonus_value'] = obv_momentum_bonus
    
    # --- 3. ボラティリティ/過熱感 ---
    
    # a. ボラティリティ過熱ペナルティ 
    volatility_penalty = 0.0
    # BBW (バンド幅) が過去N期間の平均を大きく超えている場合 (過熱=短期的な反転リスク)
    # ここでは単純に絶対閾値 0.01 (1%) を使用
    if last['BBW'] > VOLATILITY_BB_PENALTY_THRESHOLD:
        volatility_penalty = -0.05 # -5点のペナルティ
        
    current_score += volatility_penalty
    score_details['volatility_penalty_value'] = volatility_penalty
    
    # --- 4. 流動性・マクロ要因 ---
    
    # a. 流動性ボーナス (最大+0.06) - 外部計算値
    current_score += liquidity_bonus
    score_details['liquidity_bonus_value'] = liquidity_bonus
    
    # b. FGIマクロ要因 (最大 +/-0.05) - 外部計算値
    fgi_proxy_bonus = macro_context.get('fgi_proxy', 0.0)
    current_score += fgi_proxy_bonus
    score_details['sentiment_fgi_proxy_bonus'] = fgi_proxy_bonus

    # c. 為替マクロ要因 (0.0)
    forex_bonus = macro_context.get('forex_bonus', 0.0)
    current_score += forex_bonus
    score_details['forex_bonus'] = forex_bonus
    
    # スコアの最終クリッピング (0.0から1.0の範囲に収める)
    final_score = max(0.0, min(1.0, current_score))
    
    return final_score, score_details


def generate_sl_tp_rr(df: pd.DataFrame, timeframe: str) -> Optional[Dict[str, float]]:
    """
    現在の価格とATRに基づいてSL/TPを決定し、RRRが1.0以上であることを確認する。
    RRRは 報酬(TP-Entry) / リスク(Entry-SL) で計算する。
    """
    if df.empty or len(df) < 1:
        return None

    last_bar = df.iloc[-1]
    entry_price = last_bar['close']
    atr_value = last_bar['ATR']
    
    if pd.isna(entry_price) or pd.isna(atr_value) or atr_value <= 0:
        return None
        
    # SL/TPは複数の要因に基づいて動的に決定される
    # 1. SL候補: 直近の安値の少し下、またはサポートライン、またはATRの倍数
    # 2. TP候補: 直近の高値の少し上、またはレジスタンスライン、またはATRの倍数

    # --- SL (ストップロス) の計算 ---
    
    # a. ATRベース (リスクの基準)
    # 1.5 ATRを下限のリスク許容度とする
    atr_risk_base = atr_value * 1.5
    
    # b. 構造ベース (直近の安値/サポート)
    # 直近N期間 (例えば10足) の最安値を構造上のSL候補とする
    low_lookback = 10
    recent_low = df['low'][-low_lookback:-1].min() # 最新の足を除く
    
    # SL価格は、より遠い方（安全側）を採用する
    sl_atr_based = entry_price - atr_risk_base
    sl_structural_based = recent_low * 0.999 # 構造安値から0.1%下のバッファ
    
    # SLはエントリー価格から十分な距離があることを確認
    # (構造ベースとATRベースでより深い方を採用)
    stop_loss = min(sl_atr_based, sl_structural_based)
    
    # SLがエントリー価格から近すぎる場合は無効とする (例えば0.5%未満)
    min_risk_percentage = 0.005 # 0.5%
    if (entry_price - stop_loss) / entry_price < min_risk_percentage:
        # ATRリスクベースで再計算し、最小リスク幅を強制
        stop_loss = entry_price * (1 - min_risk_percentage)
        
    risk_absolute = entry_price - stop_loss
    
    if risk_absolute <= 0:
        return None

    # --- TP (テイクプロフィット) の計算 ---
    
    # RRR (リスクリワード比率) の目標は最低1.0。
    # ここでは積極的なR:Rとして 1.5 ATR (リスク幅) の 1.5倍のリワードを狙う
    # リワードをリスク絶対値の1.5倍に設定
    reward_absolute = risk_absolute * 1.5
    
    # RRR = Reward / Risk
    rr_ratio = reward_absolute / risk_absolute 
    
    # TP価格の計算
    take_profit = entry_price + reward_absolute

    # 構造上のTP上限をチェック
    # R1をTPの上限候補とする (安全策)
    structural_tp_limit = last_bar['R1']
    
    # TP価格がR1を超える場合は、R1を採用 (安全を優先)
    if take_profit > structural_tp_limit:
        take_profit = structural_tp_limit
        
        # TPをR1に調整した後、再度RRRを確認
        new_reward_absolute = take_profit - entry_price
        
        # TP調整後にRRRが1.0を下回る場合はシグナル不採用
        if new_reward_absolute / risk_absolute < 1.0:
            return None # RRR < 1.0 は取引不可
            
        rr_ratio = new_reward_absolute / risk_absolute
        
    # TPがSLより下になったり、TPとSLが近すぎるなど、無効な設定をチェック
    if take_profit <= entry_price or stop_loss >= entry_price or take_profit <= stop_loss:
         return None

    # 最終的なR:Rが1.0以上であることを確認
    if rr_ratio < 1.0:
        return None


    return {
        'entry_price': entry_price,
        'stop_loss': stop_loss,
        'take_profit': take_profit,
        'risk_absolute': risk_absolute,
        'reward_absolute': reward_absolute,
        'rr_ratio': rr_ratio
    }


def get_dynamic_threshold(macro_context: Dict) -> float:
    """マクロコンテキストに基づいて動的な取引閾値を決定する"""
    fgi_proxy = macro_context.get('fgi_proxy', 0.0)
    
    if fgi_proxy <= FGI_SLUMP_THRESHOLD:
        # 低迷市場 (-0.02以下) -> 閾値: 0.70 (最も厳しい)
        return SIGNAL_THRESHOLD_SLUMP
    elif fgi_proxy >= FGI_ACTIVE_THRESHOLD:
        # 活発市場 (+0.02以上) -> 閾値: 0.60 (最も緩い)
        return SIGNAL_THRESHOLD_ACTIVE
    else:
        # 通常市場 -> 閾値: 0.65
        return SIGNAL_THRESHOLD_NORMAL


async def find_long_signals(
    symbol: str, 
    macro_context: Dict, 
    market_liquidity: float # 0.0 to LIQUIDITY_BONUS_MAX
) -> List[Dict]:
    """
    指定されたシンボルの複数の時間枠でロングシグナルを探す
    """
    
    signals = []
    
    for timeframe in TARGET_TIMEFRAMES:
        limit = REQUIRED_OHLCV_LIMITS[timeframe]
        
        # 1. データ取得
        df = await fetch_ohlcv_with_fallback(symbol, timeframe, limit)
        if df is None or len(df) < limit:
            logging.warning(f"⚠️ {symbol} ({timeframe}): データ不足。スキップします。")
            continue

        # 2. インジケータ計算
        df = calculate_technical_indicators(df)
        if df.empty:
            logging.debug(f"ℹ️ {symbol} ({timeframe}): インジケータ計算後のデータが空。スキップ。")
            continue

        # 3. RRRとSL/TPの計算
        rr_details = generate_sl_tp_rr(df, timeframe)
        if rr_details is None or rr_details['rr_ratio'] < 1.0:
            # logging.debug(f"ℹ️ {symbol} ({timeframe}): RRR < 1.0。スキップします。")
            continue

        # 4. スコアリング
        score, tech_data = analyze_and_score_long(df, timeframe, macro_context, market_liquidity)

        # 5. シグナルとして統合
        signal = {
            'symbol': symbol,
            'timeframe': timeframe,
            'score': score,
            'rr_ratio': rr_details['rr_ratio'],
            'entry_price': rr_details['entry_price'],
            'stop_loss': rr_details['stop_loss'],
            'take_profit': rr_details['take_profit'],
            'tech_data': tech_data,
            # その他の情報 (ロギング用)
            'current_time': datetime.now(JST).isoformat()
        }
        signals.append(signal)

    return signals

# ====================================================================================
# TRADING EXECUTION & POSITION MANAGEMENT
# ====================================================================================

async def get_market_liquidity(symbol: str) -> float:
    """
    板情報から流動性/市場の厚みを評価し、ボーナスポイントを計算する (0.0 to LIQUIDITY_BONUS_MAX)
    """
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT:
        return 0.0
        
    try:
        # 深度情報 (Limit 10) を取得
        order_book = await EXCHANGE_CLIENT.fetch_order_book(symbol, limit=10)
        
        # 板の厚み (Bid/Ask) をUSDT換算で計算 (上位3層の合計を使用)
        bid_volume_usdt = 0.0
        ask_volume_usdt = 0.0
        
        # Bid (買い板) の厚みを計算
        if order_book['bids']:
            for price, amount in order_book['bids'][:3]:
                bid_volume_usdt += price * amount
        
        # Ask (売り板) の厚みを計算
        if order_book['asks']:
            for price, amount in order_book['asks'][:3]:
                ask_volume_usdt += price * amount
                
        total_liquidity_usdt = bid_volume_usdt + ask_volume_usdt
        
        # 流動性評価 (例: 100,000 USDT以上の流動性があれば最大ボーナス)
        # 調整係数: 50,000 USDTで最大ボーナスとなるように設定
        max_liquidity_for_bonus = 50000.0
        liquidity_ratio = min(1.0, total_liquidity_usdt / max_liquidity_for_bonus)
        
        # ボーナスポイントの計算
        liquidity_bonus = liquidity_ratio * LIQUIDITY_BONUS_MAX

        return liquidity_bonus
        
    except Exception as e:
        logging.warning(f"⚠️ {symbol} の流動性取得エラー: {e}")
        return 0.0 # エラー時はボーナスなし

def calculate_dynamic_trade_size(signal: Dict, account_status: Dict) -> Tuple[float, float]:
    """
    リスクベースで動的な取引サイズ (ロットサイズ) を計算する。
    最大リスク許容度とBASE_TRADE_SIZE_USDTを考慮。
    
    戻り値: (ロットサイズUSDT, リスク絶対額USDT)
    """
    
    entry_price = signal['entry_price']
    stop_loss = signal['stop_loss']
    
    # 1. 1ロットあたりの絶対リスク額 (USDT) を計算
    # 価格が下落したときの損失率
    risk_percentage = (entry_price - stop_loss) / entry_price
    
    # リスク絶対額は、BASE_TRADE_SIZE_USDT * リスクパーセンテージに相当
    # ただし、取引サイズをBASE_TRADE_SIZE_USDTに固定するため、ここはBASE_TRADE_SIZE_USDTを使用。
    trade_size_usdt = BASE_TRADE_SIZE_USDT 
    
    # 2. リスク絶対額 (R) の計算 (BASE_TRADE_SIZE_USDTに対するリスク)
    # R = TradeSize * RiskPercentage
    risk_absolute_usdt = trade_size_usdt * risk_percentage
    
    # 3. 口座残高に基づく調整 (最大リスク) - 未実装だが将来的な拡張ポイント
    # 例: 総資産の1%を最大リスクとする

    # 4. 最小取引額のチェック
    # CCXTの精度情報が必要だが、ここではBASE_TRADE_SIZE_USDTが最小取引額 (例: 10 USDT) より大きいことを前提とする
    
    return trade_size_usdt, risk_absolute_usdt

async def execute_trade(signal: Dict, trade_size_usdt: float) -> Dict:
    """現物取引 (スポット市場) でロング注文を実行する"""
    global EXCHANGE_CLIENT
    
    symbol = signal['symbol']
    entry_price = signal['entry_price']
    
    trade_result = {
        'status': 'error',
        'error_message': '取引所クライアントが未初期化です。',
        'filled_amount': 0.0,
        'filled_usdt': 0.0,
        'entry_price': entry_price,
    }
    
    if not EXCHANGE_CLIENT:
        return trade_result

    if TEST_MODE:
        trade_result['status'] = 'ok'
        trade_result['error_message'] = 'TEST_MODE: 取引実行をスキップしました。'
        trade_result['filled_amount'] = trade_size_usdt / entry_price
        trade_result['filled_usdt'] = trade_size_usdt
        return trade_result
        
    try:
        # 1. 取引ペアの最小取引量と精度を取得
        market = EXCHANGE_CLIENT.markets[symbol]
        
        # 数量精度 (amount precision)
        amount_precision = market['precision']['amount'] if 'amount' in market['precision'] else 8
        
        # 最小取引量 (最小発注数量)
        min_amount = market['limits']['amount']['min'] if 'amount' in market['limits'] and 'min' in market['limits']['amount'] else 0.0
        
        # 最小発注額 (USDT建て) - MEXCでは'cost'/'min' (例: 10 USDT)
        min_cost = market['limits']['cost']['min'] if 'cost' in market['limits'] and 'min' in market['limits']['cost'] else 10.0


        # 2. 発注数量を計算
        # 注文の目標数量 (Base Currency)
        amount_base = trade_size_usdt / entry_price 
        
        # 数量を取引所が許容する精度に丸める
        # ccxtのround_to_precision関数を使用
        amount = EXCHANGE_CLIENT.amount_to_precision(symbol, amount_base)
        
        amount_float = float(amount)

        # 3. 最小取引量のチェック (USDT建てとBase通貨建て)
        if trade_size_usdt < min_cost:
             trade_result['error_message'] = f"最小取引額 ({min_cost:.2f} USDT) 未満です ({trade_size_usdt:.2f} USDT)。"
             return trade_result
             
        if amount_float < min_amount:
            trade_result['error_message'] = f"最小取引数量 ({min_amount:.8f} {symbol.split('/')[0]}) 未満です ({amount_float:.8f})。"
            return trade_result


        # 4. 成行買い注文の実行 (Spot Market Buy)
        # ccxtのcreate_market_buy_order_with_costメソッドを使用 (USDT建てで発注できる)
        # MEXCなど一部の取引所では、create_market_buy_order_with_costが使えない場合があるため、
        # ここではcreate_orderを使用し、数量ベースで発注。
        # amount (Base通貨の数量)で発注
        order = await EXCHANGE_CLIENT.create_order(
            symbol=symbol,
            type='market',
            side='buy',
            amount=amount_float
        )

        # 5. 約定結果の確認
        if order and order['status'] == 'closed':
            filled_amount = order['filled']
            filled_usdt = order['cost'] # 約定に使用されたUSDTコスト
            
            trade_result['status'] = 'ok'
            trade_result['error_message'] = None
            trade_result['filled_amount'] = filled_amount
            trade_result['filled_usdt'] = filled_usdt
            trade_result['entry_price'] = filled_usdt / filled_amount if filled_amount else entry_price

            logging.info(f"✅ {symbol}: ロング注文成功。数量: {filled_amount:.4f}, コスト: {filled_usdt:.2f} USDT")
            
            # 6. ポジションリストへの追加
            OPEN_POSITIONS.append({
                'id': str(uuid.uuid4()), # ユニークID
                'symbol': symbol,
                'entry_price': trade_result['entry_price'],
                'filled_amount': filled_amount,
                'filled_usdt': filled_usdt,
                'stop_loss': signal['stop_loss'],
                'take_profit': signal['take_profit'],
                'rr_ratio': signal['rr_ratio'],
                'timeframe': signal['timeframe'],
                'signal_score': signal['score'],
                'open_time': time.time()
            })
            
        elif order and order['status'] == 'open':
             # 一部約定/未約定の残骸が残る可能性 (基本的には成行なのでClosedになるはず)
             trade_result['error_message'] = f"注文が完全には約定しませんでした。ステータス: {order['status']}"
             logging.warning(f"⚠️ {symbol}: 注文未完了。ステータス: {order['status']}")
        else:
            trade_result['error_message'] = f"注文に失敗しました (CCXTレスポンスなしまたは不明なステータス)。"
            logging.error(f"❌ {symbol}: 注文失敗。レスポンス: {order}")

    except ccxt.NetworkError as e:
        trade_result['error_message'] = f"ネットワークエラー: {str(e)}"
        logging.error(f"❌ {symbol}: CCXTネットワークエラー: {e}")
    except ccxt.ExchangeError as e:
        trade_result['error_message'] = f"取引所エラー: {str(e)}"
        logging.error(f"❌ {symbol}: CCXT取引所エラー: {e}")
    except Exception as e:
        trade_result['error_message'] = f"予期せぬエラー: {str(e)}"
        logging.error(f"❌ {symbol}: 予期せぬエラー: {e}", exc_info=True)
        
    return trade_result

async def close_position(position: Dict, exit_price: float, exit_type: str) -> Dict:
    """現物ポジションを成行で決済する (全量をUSDTに売却)"""
    global EXCHANGE_CLIENT
    
    symbol = position['symbol']
    amount_to_sell = position['filled_amount']
    entry_price = position['entry_price']
    filled_usdt = position['filled_usdt']
    
    exit_result = {
        'status': 'error',
        'error_message': '取引所クライアントが未初期化です。',
        'pnl_usdt': 0.0,
        'pnl_rate': 0.0,
        'exit_price': exit_price,
        'entry_price': entry_price,
        'filled_amount': amount_to_sell,
    }

    if not EXCHANGE_CLIENT:
        return exit_result

    if TEST_MODE:
        # テストモードでは損益計算のみ実行
        cost_at_exit = amount_to_sell * exit_price
        pnl_usdt = cost_at_exit - filled_usdt
        pnl_rate = pnl_usdt / filled_usdt if filled_usdt else 0.0

        exit_result['status'] = 'ok'
        exit_result['error_message'] = 'TEST_MODE: 決済実行をスキップしました。'
        exit_result['pnl_usdt'] = pnl_usdt
        exit_result['pnl_rate'] = pnl_rate
        return exit_result
        
    try:
        # 1. 数量精度を取得
        market = EXCHANGE_CLIENT.markets[symbol]
        
        # 数量を取引所が許容する精度に丸める
        amount = EXCHANGE_CLIENT.amount_to_precision(symbol, amount_to_sell)
        amount_float = float(amount)
        
        # 2. 成行売り注文の実行 (Spot Market Sell)
        order = await EXCHANGE_CLIENT.create_order(
            symbol=symbol,
            type='market',
            side='sell',
            amount=amount_float
        )

        # 3. 約定結果の確認
        if order and order['status'] == 'closed':
            exit_amount = order['filled']
            cost_at_exit = order['cost'] # 約定で得られたUSDT (手数料考慮前)
            
            # 手数料を取得 (正確な手数料は注文情報から取得するのが理想だが、ここでは簡略化)
            fees = order.get('fees', [])
            total_fee_usdt = sum(fee['cost'] for fee in fees if fee['currency'] == 'USDT')
            
            # 実質の受取USDT
            net_received_usdt = cost_at_exit - total_fee_usdt
            
            # 損益計算
            pnl_usdt = net_received_usdt - filled_usdt
            pnl_rate = pnl_usdt / filled_usdt if filled_usdt else 0.0

            exit_result['status'] = 'ok'
            exit_result['error_message'] = None
            exit_result['pnl_usdt'] = pnl_usdt
            exit_result['pnl_rate'] = pnl_rate
            exit_result['exit_price'] = net_received_usdt / exit_amount if exit_amount else exit_price
            
            logging.info(f"✅ {symbol}: 決済成功 ({exit_type})。損益: {pnl_usdt:.2f} USDT ({pnl_rate*100:.2f}%)")
            
        else:
            exit_result['error_message'] = f"注文が完全には約定しませんでした。ステータス: {order['status']}"
            logging.warning(f"⚠️ {symbol}: 決済未完了。ステータス: {order['status']}")


    except ccxt.NetworkError as e:
        exit_result['error_message'] = f"ネットワークエラー: {str(e)}"
        logging.error(f"❌ {symbol}: CCXTネットワークエラー: {e}")
    except ccxt.ExchangeError as e:
        exit_result['error_message'] = f"取引所エラー: {str(e)}"
        logging.error(f"❌ {symbol}: CCXT取引所エラー: {e}")
    except Exception as e:
        exit_result['error_message'] = f"予期せぬエラー: {str(e)}"
        logging.error(f"❌ {symbol}: 予期せぬエラー: {e}", exc_info=True)
        
    return exit_result

async def monitor_positions_loop():
    """保有中のポジションを監視し、SL/TPに達したら決済するループ"""
    global OPEN_POSITIONS

    while True:
        try:
            # 1. 現在の価格情報をまとめて取得 (USDTペア)
            symbols_to_monitor = [p['symbol'] for p in OPEN_POSITIONS]
            if not symbols_to_monitor:
                await asyncio.sleep(MONITOR_INTERVAL)
                continue
                
            tickers = await EXCHANGE_CLIENT.fetch_tickers(symbols_to_monitor)
            current_prices = {s: t['last'] for s, t in tickers.items() if 'last' in t}
            
            positions_to_close = []
            
            # 2. 各ポジションをチェック
            for position in OPEN_POSITIONS:
                symbol = position['symbol']
                current_price = current_prices.get(symbol)
                
                if current_price is None:
                    logging.warning(f"⚠️ {symbol}: 最新の価格が取得できませんでした。スキップします。")
                    continue
                    
                stop_loss = position['stop_loss']
                take_profit = position['take_profit']
                
                exit_type = None
                
                # SLチェック (価格がSLを下回った)
                if current_price <= stop_loss:
                    exit_type = "SL (ストップロス)"
                    
                # TPチェック (価格がTPを上回った)
                elif current_price >= take_profit:
                    exit_type = "TP (テイクプロフィット)"
                    
                # 決済対象であればリストに追加
                if exit_type:
                    positions_to_close.append((position, current_price, exit_type))

            
            # 3. 決済の実行 (同期処理を避けるため、別タスクで順次実行)
            close_tasks = []
            positions_to_remove = []
            
            for position, exit_price, exit_type in positions_to_close:
                close_tasks.append(
                    asyncio.create_task(
                        _handle_position_close(position, exit_price, exit_type)
                    )
                )

            # 4. 決済が完了したポジションをリストから削除
            if close_tasks:
                # 実行結果を待つ (必要であれば)
                await asyncio.gather(*close_tasks)
                
                # 削除処理: 決済対象リストに基づいて元のリストから削除
                # IDをキーに削除することで、競合を避ける
                closed_ids = [p['id'] for p, _, _ in positions_to_close]
                OPEN_POSITIONS[:] = [p for p in OPEN_POSITIONS if p['id'] not in closed_ids]
                

        except Exception as e:
            logging.error(f"❌ ポジション監視ループで予期せぬエラー: {e}", exc_info=True)
            
        await asyncio.sleep(MONITOR_INTERVAL)


async def _handle_position_close(position: Dict, exit_price: float, exit_type: str):
    """個別のポジション決済とログ/通知処理を行うヘルパー関数"""
    symbol = position['symbol']
    logging.info(f"🚨 {symbol}: ポジション決済トリガー ({exit_type}) - 価格: {exit_price:.8f}")

    exit_result = await close_position(position, exit_price, exit_type)
    
    # 決済通知用のシグナルデータを再構築 (スコアなどはポジションオープン時のものを使用)
    signal_for_notification = {
        'symbol': position['symbol'],
        'timeframe': position['timeframe'],
        'score': position['signal_score'],
        'rr_ratio': position['rr_ratio'],
        # entry/sl/tpはポジション情報から取得
        'entry_price': position['entry_price'], 
        'stop_loss': position['stop_loss'],
        'take_profit': position['take_profit'],
    }

    if exit_result['status'] == 'ok':
        # 成功ログと通知
        log_signal(signal_for_notification, "Trade Exit", exit_result)
        message = format_telegram_message(
            signal=signal_for_notification,
            context="ポジション決済",
            current_threshold=get_dynamic_threshold(GLOBAL_MACRO_CONTEXT),
            trade_result=exit_result,
            exit_type=exit_type
        )
        await send_telegram_notification(message)
    else:
        # 失敗ログ (取引所エラー)
        logging.error(f"❌ {symbol}: 決済失敗。エラー: {exit_result['error_message']}")
        error_message = f"❌ {symbol}: 決済({exit_type})失敗。手動で確認してください。エラー: {exit_result['error_message']}"
        await send_telegram_notification(error_message)


# ====================================================================================
# MAIN LOOPS
# ====================================================================================

async def main_loop():
    """取引シグナルを見つけて取引を実行するメインループ"""
    global LAST_SUCCESS_TIME, LAST_SIGNAL_TIME, LAST_ANALYSIS_SIGNALS, GLOBAL_MACRO_CONTEXT, IS_FIRST_MAIN_LOOP_COMPLETED
    
    # 起動直後にマクロ情報を取得
    GLOBAL_MACRO_CONTEXT = await get_crypto_macro_context()

    while True:
        start_time = time.time()
        logging.info("--- メイン分析ループ開始 ---")
        
        try:
            # 1. 監視銘柄リストを更新 (約10分ごと)
            await update_symbols_by_volume()
            
            # 2. グローバルマクロコンテキストを更新
            GLOBAL_MACRO_CONTEXT = await get_crypto_macro_context()
            current_threshold = get_dynamic_threshold(GLOBAL_MACRO_CONTEXT)
            logging.info(f"🌍 市場環境スコア: FGI={GLOBAL_MACRO_CONTEXT['fgi_raw_value']} ({GLOBAL_MACRO_CONTEXT['fgi_proxy']*100:.2f}点), 動的閾値: {current_threshold*100:.1f}点")
            
            all_signals: List[Dict] = []
            
            # 3. 全監視銘柄に対して分析を実行
            tasks = []
            for symbol in CURRENT_MONITOR_SYMBOLS:
                # 各シンボルの流動性を事前に取得
                market_liquidity = await get_market_liquidity(symbol)
                
                # シグナル検出タスクを作成
                tasks.append(find_long_signals(symbol, GLOBAL_MACRO_CONTEXT, market_liquidity))

            # 全タスクを並行実行
            results = await asyncio.gather(*tasks)
            
            for result_list in results:
                all_signals.extend(result_list)
                
            # 4. シグナルをスコア降順にソートし、分析結果をグローバル変数に保存
            all_signals.sort(key=lambda x: x['score'], reverse=True)
            LAST_ANALYSIS_SIGNALS = all_signals

            # 5. 取引シグナルを評価し、実行
            executed_count = 0
            
            for signal in all_signals:
                symbol = signal['symbol']
                score = signal['score']
                
                # クールダウンチェック: 過去2時間以内に通知していないか
                if symbol in LAST_SIGNAL_TIME and (time.time() - LAST_SIGNAL_TIME[symbol] < TRADE_SIGNAL_COOLDOWN):
                    logging.debug(f"ℹ️ {symbol}: クールダウン中。スキップします。")
                    continue
                    
                # スコアチェック: 動的閾値を超えているか and RRRが1.0以上
                if score >= current_threshold and signal['rr_ratio'] >= 1.0:
                    
                    if executed_count >= TOP_SIGNAL_COUNT:
                        logging.info("ℹ️ 最大取引シグナル数に達しました。")
                        break
                        
                    # 取引サイズと絶対リスクを計算
                    account_status = await fetch_account_status()
                    trade_size_usdt, risk_absolute_usdt = calculate_dynamic_trade_size(signal, account_status)
                    signal['lot_size_usdt'] = trade_size_usdt # 実行前にシグナルデータに追加

                    logging.info(f"🔥 {symbol} ({signal['timeframe']}): シグナル検出 (Score: {score:.4f} / Threshold: {current_threshold:.4f})")

                    # 取引実行
                    trade_result = await execute_trade(signal, trade_size_usdt)
                    
                    # 取引ログと通知
                    log_type = "Trade Signal"
                    log_signal(signal, log_type, trade_result)
                    
                    message = format_telegram_message(
                        signal=signal, 
                        context="取引シグナル", 
                        current_threshold=current_threshold, 
                        trade_result=trade_result
                    )
                    await send_telegram_notification(message)
                    
                    # 成功した場合のみカウントとクールダウンを更新
                    if trade_result['status'] == 'ok':
                        LAST_SIGNAL_TIME[symbol] = time.time()
                        executed_count += 1
                
            LAST_SUCCESS_TIME = time.time()
            
            # 初回起動通知の送信
            if not IS_FIRST_MAIN_LOOP_COMPLETED:
                account_status = await fetch_account_status()
                startup_message = format_startup_message(
                    account_status=account_status,
                    macro_context=GLOBAL_MACRO_CONTEXT,
                    monitoring_count=len(CURRENT_MONITOR_SYMBOLS),
                    current_threshold=current_threshold,
                    bot_version="v19.0.28 - Safety and Frequency Finalized (Patch 36)"
                )
                await send_telegram_notification(startup_message)
                IS_FIRST_MAIN_LOOP_COMPLETED = True
            
        except Exception as e:
            logging.critical(f"❌ メインループで致命的なエラー: {e}", exc_info=True)
            # エラー発生時もクールダウンを実施
            
        finally:
            end_time = time.time()
            duration = end_time - start_time
            sleep_duration = max(0, LOOP_INTERVAL - duration)
            logging.info(f"--- メイン分析ループ完了。所要時間: {duration:.2f}秒。次回実行まで {sleep_duration:.0f}秒待機。 ---")
            await asyncio.sleep(sleep_duration)


async def analysis_only_notification_loop():
    """1時間ごとの分析専用通知を送信するループ"""
    global LAST_ANALYSIS_ONLY_NOTIFICATION_TIME, LAST_ANALYSIS_SIGNALS, GLOBAL_MACRO_CONTEXT
    
    while True:
        current_time = time.time()
        
        if current_time - LAST_ANALYSIS_ONLY_NOTIFICATION_TIME >= ANALYSIS_ONLY_INTERVAL:
            logging.info("--- 分析専用通知ループ開始 ---")
            
            try:
                # メインループの最新の結果を使用
                current_threshold = get_dynamic_threshold(GLOBAL_MACRO_CONTEXT)
                
                # 通知メッセージの生成
                notification_message = format_analysis_only_message(
                    all_signals=LAST_ANALYSIS_SIGNALS,
                    macro_context=GLOBAL_MACRO_CONTEXT,
                    current_threshold=current_threshold,
                    monitoring_count=len(CURRENT_MONITOR_SYMBOLS)
                )
                
                # ログ記録 (分析結果の最高スコアのみを抽出)
                top_signal = LAST_ANALYSIS_SIGNALS[0] if LAST_ANALYSIS_SIGNALS else {}
                log_signal(top_signal, "Hourly Analysis")

                # Telegram送信
                await send_telegram_notification(notification_message)
                
                LAST_ANALYSIS_ONLY_NOTIFICATION_TIME = current_time
                
            except Exception as e:
                logging.error(f"❌ 分析専用通知ループでエラー: {e}", exc_info=True)
                
        await asyncio.sleep(60) # 1分ごとにチェック


async def webshare_upload_loop():
    """1時間ごとのログアップロードを処理するループ"""
    global LAST_WEBSHARE_UPLOAD_TIME
    
    while True:
        current_time = time.time()
        
        if current_time - LAST_WEBSHARE_UPLOAD_TIME >= WEBSHARE_UPLOAD_INTERVAL:
            if WEBSHARE_HOST:
                await upload_logs_to_webshare()
                LAST_WEBSHARE_UPLOAD_TIME = current_time
            else:
                logging.debug("ℹ️ WebShare設定がないため、ログアップロードをスキップ。")
                
        await asyncio.sleep(60 * 10) # 10分ごとにチェック

# ====================================================================================
# FASTAPI / APPLICATION ENTRY POINT
# ====================================================================================

# FastAPIアプリケーションの初期化
app = FastAPI(title="Apex BOT API", version="v19.0.28")

@app.on_event("startup")
async def startup_event():
    # 1. CCXTクライアントの初期化
    success = await initialize_exchange_client()
    if not success:
        logging.critical("🚨 CCXTクライアントの初期化に失敗しました。BOTを起動できません。")
        sys.exit(1)

    # 2. 初期銘柄リストをロード/更新
    await update_symbols_by_volume()
    
    # 3. 各種ループの初回実行時間を調整 (起動直後に実行開始)
    global LAST_SUCCESS_TIME, LAST_ANALYSIS_ONLY_NOTIFICATION_TIME, LAST_WEBSHARE_UPLOAD_TIME
    
    # メインループをすぐに開始
    LAST_SUCCESS_TIME = time.time() - LOOP_INTERVAL
    
    # 初回起動後15分後に分析専用通知
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
        "last_success_time_utc": datetime.fromtimestamp(LAST_SUCCESS_TIME, tz=timezone.utc).isoformat() if LAST_SUCCESS_TIME else "N/A",
        "next_main_loop_check_sec": max(0, int(LAST_SUCCESS_TIME + LOOP_INTERVAL - time.time())) if LAST_SUCCESS_TIME else LOOP_INTERVAL,
        "current_macro_fgi": GLOBAL_MACRO_CONTEXT.get('fgi_raw_value', 'N/A'),
    }
    return JSONResponse(content=status_msg)

# 💡 【追加】UptimeRobot ヘルスチェック用エンドポイント (HTTP GET)
@app.get("/health")
def get_health_check():
    """UptimeRobotなどの外部監視サービス向けヘルスチェック。BOTが稼働中であれば200 OKを返す。"""
    # BOTが起動し、FastAPIがリクエストを受け付けていることを確認します。
    # 内部ロジックの状態確認は /status に任せ、ここではシンプルな稼働確認に徹します。
    if IS_FIRST_MAIN_LOOP_COMPLETED and LAST_SUCCESS_TIME > 0:
        return {"status": "ok", "message": "Apex BOT is running and the main loop has completed at least once."}
    else:
        # 初回ループ完了前でも、アプリケーションがリクエストに応答しているため 200 OKを返します
        return {"status": "running", "message": "Apex BOT is active."}, 200


if __name__ == "__main__":
    # uvicornでFastAPIアプリケーションを実行
    logging.info("🤖 Apex BOT v19.0.28 (Patch 36) - 起動中...")
    # ホスト '0.0.0.0' で実行することで、外部からのアクセスを許可
    uvicorn.run(app, host="0.0.0.0", port=8000)
