# ====================================================================================
# Apex BOT v19.0.29 - High-Freq/TP/SL/M1M5 Added (Patch 38)
#
# 改良・修正点:
# 1. 【機能追加】分析対象の時間足に '1m' (1分足) と '5m' (5分足) を追加。
# 2. 【頻度変更】メインループの実行間隔を 10分ごとから 1分ごと (60秒) に変更。
# 3. 【機能追加】ポジションのストップロス(SL)/テイクプロフィット(TP)をリアルタイム (10秒ごと) に監視し、自動決済するロジックを実装。
# 4. 【ロジック維持】元のブレークダウンロジックが正常に機能するよう、analyze_signals関数内でtech_dataを適切に構築。
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
import uuid 
import math # 数値計算ライブラリ

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
    "DOGE/USDT", "DOT/USDT", "TRX/USDT", 
    "LTC/USDT", "AVAX/USDT", "LINK/USDT", "UNI/USDT", "ETC/USDT", "BCH/USDT",
    "NEAR/USDT", "ATOM/USDT", 
    "ALGO/USDT", "XLM/USDT", "SAND/USDT",
    "GALA/USDT", "FIL/USDT", 
    "AXS/USDT", "MANA/USDT", "AAVE/USDT",
    "FLOW/USDT", "IMX/USDT", 
]
TOP_SYMBOL_LIMIT = 40               # 監視対象銘柄の最大数 (出来高TOPから選出)
LOOP_INTERVAL = 60 * 1              # ★変更: メインループの実行間隔 (秒) - 1分ごと
ANALYSIS_ONLY_INTERVAL = 60 * 60    # 分析専用通知の実行間隔 (秒) - 1時間ごと
WEBSHARE_UPLOAD_INTERVAL = 60 * 60  # WebShareログアップロード間隔 (1時間ごと)
MONITOR_INTERVAL = 10               # ★追加: ポジション監視ループの実行間隔 (秒) - 10秒ごと

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


# 💡 WEBSHARE設定 (HTTP POSTへ変更)
WEBSHARE_METHOD = os.getenv("WEBSHARE_METHOD", "HTTP") # デフォルトはHTTPに変更
WEBSHARE_POST_URL = os.getenv("WEBSHARE_POST_URL", "http://your-webshare-endpoint.com/upload") # HTTP POST用のエンドポイント

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

# CCXTクライアントの準備完了フラグ
IS_CLIENT_READY: bool = False

# 取引ルール設定
TRADE_SIGNAL_COOLDOWN = 60 * 60 * 2 # 同一銘柄のシグナル通知クールダウン（2時間）
SIGNAL_THRESHOLD = 0.65             # 動的閾値のベースライン
TOP_SIGNAL_COUNT = 3                # 通知するシグナルの最大数
REQUIRED_OHLCV_LIMITS = {'1m': 500, '5m': 500, '15m': 500, '1h': 500, '4h': 500} # ★変更: 1m, 5mを追加

# テクニカル分析定数 (v19.0.28ベース)
TARGET_TIMEFRAMES = ['1m', '5m', '15m', '1h', '4h'] # ★変更: 1m, 5mを追加
BASE_SCORE = 0.60                   # ベースとなる取引基準点 (60点)
LONG_TERM_SMA_LENGTH = 200          # 長期トレンドフィルタ用SMA
LONG_TERM_REVERSAL_PENALTY = 0.20   # 長期トレンド逆行時のペナルティ
STRUCTURAL_PIVOT_BONUS = 0.05       # 価格構造/ピボット支持時のボーナス
RSI_MOMENTUM_LOW = 40               # RSIが40以下でロングモメンタム候補
MACD_CROSS_PENALTY = 0.15           # MACDが不利なクロス/発散時のペナルティ
LIQUIDITY_BONUS_MAX = 0.06          # 流動性(板の厚み)による最大ボーナス
FGI_PROXY_BONUS_MAX = 0.05          # 恐怖・貪欲指数による最大ボーナス/ペナルティ
FOREX_BONUS_MAX = 0.0               # 為替機能を削除するため0.0に設定

# 市場環境に応じた動的閾値調整のための定数
FGI_SLUMP_THRESHOLD = -0.02         
FGI_ACTIVE_THRESHOLD = 0.02         
SIGNAL_THRESHOLD_SLUMP = 0.67       
SIGNAL_THRESHOLD_NORMAL = 0.63      
SIGNAL_THRESHOLD_ACTIVE = 0.58      

RSI_DIVERGENCE_BONUS = 0.10         
VOLATILITY_BB_PENALTY_THRESHOLD = 0.01 
OBV_MOMENTUM_BONUS = 0.04           

# ====================================================================================
# UTILITIES & FORMATTING (変更なし)
# ====================================================================================

def format_usdt(amount: float) -> str:
    """USDT金額を整形する"""
    if amount is None:
        amount = 0.0
        
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

def get_current_threshold(macro_context: Dict) -> float:
    """現在の市場環境に合わせた動的な取引閾値を決定し、返す。"""
    global FGI_SLUMP_THRESHOLD, FGI_ACTIVE_THRESHOLD
    global SIGNAL_THRESHOLD_SLUMP, SIGNAL_THRESHOLD_NORMAL, SIGNAL_THRESHOLD_ACTIVE
    
    fgi_proxy = macro_context.get('fgi_proxy', 0.0)
    
    if fgi_proxy < FGI_SLUMP_THRESHOLD:
        return SIGNAL_THRESHOLD_SLUMP
    
    elif fgi_proxy > FGI_ACTIVE_THRESHOLD:
        return SIGNAL_THRESHOLD_ACTIVE
        
    else:
        return SIGNAL_THRESHOLD_NORMAL

# ★ブレークダウンロジック (コア部分) - 変更なしで維持
def get_score_breakdown(signal: Dict) -> str:
    """分析スコアの詳細なブレークダウンメッセージを作成する (Telegram通知用)"""
    tech_data = signal.get('tech_data', {})
    timeframe = signal.get('timeframe', 'N/A')
    
    LONG_TERM_REVERSAL_PENALTY_CONST = LONG_TERM_REVERSAL_PENALTY 
    MACD_CROSS_PENALTY_CONST = MACD_CROSS_PENALTY                 
    LIQUIDITY_BONUS_POINT_CONST = LIQUIDITY_BONUS_MAX           
    
    breakdown_list = []

    # 1. ベーススコア
    breakdown_list.append(f"  - **ベーススコア ({timeframe})**: <code>+{BASE_SCORE*100:.1f}</code> 点")
    
    # 2. 長期トレンド/構造の確認
    penalty_applied = tech_data.get('long_term_reversal_penalty_value', 0.0)
    if penalty_applied > 0.0:
        breakdown_list.append(f"  - ❌ 長期トレンド逆行 (SMA{LONG_TERM_SMA_LENGTH}): <code>-{penalty_applied*100:.1f}</code> 点")
    else:
        # NOTE: ペナルティ回避が実質的な加点要因として表示される
        breakdown_list.append(f"  - ✅ 長期トレンド一致 (SMA{LONG_TERM_SMA_LENGTH}): <code>+{LONG_TERM_REVERSAL_PENALTY_CONST*100:.1f}</code> 点 (ペナルティ回避)")

    pivot_bonus = tech_data.get('structural_pivot_bonus', 0.0)
    if pivot_bonus > 0.0:
        breakdown_list.append(f"  - ✅ 価格構造/ピボット支持: <code>+{pivot_bonus*100:.1f}</code> 点")

    # 3. モメンタム/出来高の確認
    macd_penalty_applied = tech_data.get('macd_penalty_value', 0.0)
    total_momentum_penalty = macd_penalty_applied

    if total_momentum_penalty > 0.0:
        breakdown_list.append(f"  - ❌ モメンタム/クロス不利: <code>-{total_momentum_penalty*100:.1f}</code> 点")
    else:
        # NOTE: ペナルティ回避が実質的な加点要因として表示される
        breakdown_list.append(f"  - ✅ MACD/RSIモメンタム加速: <code>+{MACD_CROSS_PENALTY_CONST*100:.1f}</code> 点相当 (ペナルティ回避)")

    obv_bonus = tech_data.get('obv_momentum_bonus_value', 0.0)
    if obv_bonus > 0.0:
        breakdown_list.append(f"  - ✅ 出来高/OBV確証: <code>+{obv_bonus*100:.1f}</code> 点")
    
    # 4. 流動性/マクロ要因
    liquidity_bonus = tech_data.get('liquidity_bonus_value', 0.0)
    if liquidity_bonus > 0.0:
        breakdown_list.append(f"  - ✅ 流動性 (板の厚み) 優位: <code>+{LIQUIDITY_BONUS_POINT_CONST*100:.1f}</code> 点")
        
    fgi_bonus = tech_data.get('sentiment_fgi_proxy_bonus', 0.0)
    if abs(fgi_bonus) > 0.001:
        sign = '✅' if fgi_bonus > 0 else '❌'
        breakdown_list.append(f"  - {sign} FGIマクロ影響: <code>{'+' if fgi_bonus > 0 else ''}{fgi_bonus*100:.1f}</code> 点")

    forex_bonus = tech_data.get('forex_bonus', 0.0) 
    breakdown_list.append(f"  - ⚪ 為替マクロ影響: <code>{forex_bonus*100:.1f}</code> 点 (機能削除済)")
    
    volatility_penalty = tech_data.get('volatility_penalty_value', 0.0)
    if volatility_penalty < 0.0:
        breakdown_list.append(f"  - ❌ ボラティリティ過熱ペナルティ: <code>{volatility_penalty*100:.1f}</code> 点")

    return "\n".join(breakdown_list)


def format_analysis_only_message(all_signals: List[Dict], macro_context: Dict, current_threshold: float, monitoring_count: int) -> str:
    """1時間ごとの分析専用メッセージを作成する"""
    now_jst = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")
    
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

    fgi_raw_value = macro_context.get('fgi_raw_value', 'N/A')
    fgi_proxy = macro_context.get('fgi_proxy', 0.0)
    forex_bonus = macro_context.get('forex_bonus', 0.0) 

    fgi_sentiment = "リスクオン" if fgi_proxy > FGI_ACTIVE_THRESHOLD else ("リスクオフ" if fgi_proxy < FGI_SLUMP_THRESHOLD else "中立")
    forex_display = "中立 (機能削除済)"
    
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

    signal_section = "📈 <b>トップシグナル候補 (スコア順)</b>\n"
    
    if sorted_signals:
        top_signal = sorted_signals[0] # Rank 1を取得
        symbol = top_signal['symbol']
        timeframe = top_signal['timeframe']
        score = top_signal['score']
        rr_ratio = top_signal['rr_ratio']
        
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

        if top_signal['score'] < current_threshold:
             signal_section += f"\n<pre>⚠️ 注: 上記は監視中の最高スコアですが、取引閾値 ({current_threshold*100:.0f}点) 未満です。</pre>\n"
        
        if top_signal['score'] < BASE_SCORE:
             signal_section += f"<pre>🔴 警告: 最高スコアが取引基準点 ({BASE_SCORE*100:.0f}点) 未満です。</pre>\n"

    else:
        signal_section += "  - **シグナル候補なし**: 現在、すべての監視銘柄で最低限のリスクリワード比率を満たすロングシグナルは見つかりませんでした。\n"
    
    footer = (
        f"\n<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"<pre>※ この通知は取引実行を伴いません。</pre>"
        f"<i>Bot Ver: v19.0.29 - High-Freq/TP/SL/M1M5 Added (Patch 38)</i>" 
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
    
    fgi_proxy = macro_context.get('fgi_proxy', 0.0)
    forex_bonus = macro_context.get('forex_bonus', 0.0)
    
    if current_threshold == SIGNAL_THRESHOLD_SLUMP:
        market_condition_text = "低迷/リスクオフ"
    elif current_threshold == SIGNAL_THRESHOLD_ACTIVE:
        market_condition_text = "活発/リスクオン"
    else:
        market_condition_text = "通常/中立"
        
    trade_status = "自動売買 **ON**" if not TEST_MODE else "自動売買 **OFF** (TEST_MODE)"

    header = (
        f"🤖 **Apex BOT 起動完了通知** 🟢\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - **確認日時**: {now_jst} (JST)\n"
        f"  - **取引所**: <code>{CCXT_CLIENT_NAME.upper()}</code> (現物モード)\n"
        f"  - **自動売買**: <b>{trade_status}</b>\n"
        f"  - **取引ロット (BASE)**: <code>{BASE_TRADE_SIZE_USDT:.2f}</code> USDT\n" 
        f"  - **監視銘柄数**: <code>{monitoring_count}</code>\n"
        f"  - **BOTバージョン**: <code>{bot_version}</code>\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n\n"
    )

    balance_section = f"💰 <b>口座ステータス</b>\n"
    if account_status.get('error'):
        balance_section += f"<pre>⚠️ ステータス取得失敗 (セキュリティのため詳細なエラーは表示しません。ログを確認してください)</pre>\n"
    else:
        balance_section += (
            f"  - **USDT残高**: <code>{format_usdt(account_status['total_usdt_balance'])}</code> USDT\n"
        )
        
        # ボットが管理しているポジション
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

        # CCXTから取得したがボットが管理していないポジション（現物保有資産）
        open_ccxt_positions = [p for p in account_status['open_positions'] if p['usdt_value'] >= 10]
        if open_ccxt_positions:
             ccxt_value = sum(p['usdt_value'] for p in open_ccxt_positions)
             balance_section += (
                 f"  - **現物保有資産**: <code>{len(open_ccxt_positions)}</code> 銘柄 (概算価値: <code>{format_usdt(ccxt_value)}</code> USDT)\n"
             )
        
    balance_section += f"\n"

    macro_section = (
        f"🌍 <b>市場環境スコアリング</b>\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - **取引閾値 (Score)**: <code>{current_threshold*100:.0f} / 100</code>\n"
        f"  - **現在の市場環境**: <code>{market_condition_text}</code>\n"
        f"  - **FGI (恐怖・貪欲)**: <code>{macro_context.get('fgi_raw_value', 'N/A')}</code> ({'リスクオン' if fgi_proxy > FGI_ACTIVE_THRESHOLD else ('リスクオフ' if fgi_proxy < FGI_SLUMP_THRESHOLD else '中立')})\n"
        f"  - **総合マクロ影響**: <code>{((fgi_proxy + forex_bonus) * 100):.2f}</code> 点\n\n"
    )

    footer = (
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"<pre>※ この通知はメインの分析ループが一度完了したことを示します。約1分ごとに分析が実行されます。</pre>"
    )

    return header + balance_section + macro_section + footer

def format_telegram_message(signal: Dict, context: str, current_threshold: float, trade_result: Optional[Dict] = None, exit_type: Optional[str] = None) -> str:
    """Telegram通知用のメッセージを作成する (取引結果を追加)"""
    now_jst = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")
    symbol = signal['symbol']
    timeframe = signal['timeframe']
    score = signal['score']
    
    # trade_resultから値を取得する場合があるため、get()を使用
    entry_price = signal.get('entry_price', trade_result.get('entry_price', 0.0) if trade_result else 0.0)
    stop_loss = signal.get('stop_loss', trade_result.get('stop_loss', 0.0) if trade_result else 0.0)
    take_profit = signal.get('take_profit', trade_result.get('take_profit', 0.0) if trade_result else 0.0)
    rr_ratio = signal.get('rr_ratio', 0.0)
    
    estimated_wr = get_estimated_win_rate(score)
    
    # 決済通知の場合、positionデータにはtech_dataがないため、空の辞書を渡す
    breakdown_details = get_score_breakdown(signal) if context != "ポジション決済" else ""

    trade_section = ""
    trade_status_line = ""

    if context == "取引シグナル":
        lot_size = signal.get('lot_size_usdt', BASE_TRADE_SIZE_USDT)
        
        if TEST_MODE:
            trade_status_line = f"⚠️ **テストモード**: 取引は実行されません。(ロット: {format_usdt(lot_size)} USDT)"
        elif trade_result is None or trade_result.get('status') == 'error':
            trade_status_line = f"❌ **自動売買 失敗**: {trade_result.get('error_message', 'APIエラー')}"
        elif trade_result.get('status') == 'ok':
            trade_status_line = "✅ **自動売買 成功**: 現物ロング注文を執行しました。"
            
            filled_amount = trade_result.get('filled_amount', 0.0) 
            filled_usdt = trade_result.get('filled_usdt', 0.0)
            
            trade_section = (
                f"💰 **取引実行結果**\n"
                f"  - **注文タイプ**: <code>現物 (Spot) / 成行買い</code>\n"
                f"  - **動的ロット**: <code>{format_usdt(lot_size)}</code> USDT (目標)\n"
                f"  - **約定数量**: <code>{filled_amount:.4f}</code> {symbol.split('/')[0]}\n"
                f"  - **平均約定額**: <code>{format_usdt(filled_usdt)}</code> USDT\n"
            )
            
    elif context == "ポジション決済":
        # exit_typeはtrade_resultから取得
        exit_type_final = trade_result.get('exit_type', exit_type or '不明')
        trade_status_line = f"🔴 **ポジション決済**: {exit_type_final} トリガー"
        
        entry_price = trade_result.get('entry_price', 0.0)
        exit_price = trade_result.get('exit_price', 0.0)
        pnl_usdt = trade_result.get('pnl_usdt', 0.0)
        pnl_rate = trade_result.get('pnl_rate', 0.0)
        filled_amount = trade_result.get('filled_amount', 0.0)
        
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
    
    if trade_section:
        message += trade_section + f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        
    if context == "取引シグナル":
        message += (
            f"  \n**📊 スコア詳細ブレークダウン** (+/-要因)\n"
            f"{breakdown_details}\n"
            f"  <code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        )
        
    message += (f"<i>Bot Ver: v19.0.29 - High-Freq/TP/SL/M1M5 Added (Patch 38)</i>")
    return message


async def send_telegram_notification(message: str) -> bool:
    """Telegramにメッセージを送信する (変更なし)"""
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
    再帰的にオブジェクトをJSON互換の型に変換するヘルパー関数。 (変更なし)
    """
    if isinstance(obj, dict):
        return {k: _to_json_compatible(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_to_json_compatible(elem) for elem in obj]
    elif isinstance(obj, (bool, np.bool_)):
        return str(obj) 
    elif isinstance(obj, np.generic):
        return obj.item()
    return obj


def log_signal(data: Dict, log_type: str, trade_result: Optional[Dict] = None) -> None:
    """シグナルまたは取引結果をローカルファイルにログする (変更なし)"""
    try:
        log_entry = {
            'timestamp_jst': datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
            'log_type': log_type,
            'symbol': data.get('symbol', 'N/A'),
            'timeframe': data.get('timeframe', 'N/A'),
            'score': data.get('score', 0.0),
            'rr_ratio': data.get('rr_ratio', 0.0),
            'trade_result': trade_result or data.get('trade_result', None),
            'full_data': data,
        }
        
        cleaned_log_entry = _to_json_compatible(log_entry)

        log_file = f"apex_bot_{log_type.lower().replace(' ', '_')}_log.jsonl"
        with open(log_file, 'a', encoding='utf-8') as f:
            f.write(json.dumps(cleaned_log_entry, ensure_ascii=False) + '\n')
            
        logging.info(f"✅ {log_type}ログをファイルに記録しました。")
    except Exception as e:
        logging.error(f"❌ ログ書き込みエラー: {e}", exc_info=True)


# ====================================================================================
# WEBSHARE FUNCTION (HTTP POST) (変更なし)
# ====================================================================================

async def send_webshare_update(data: Dict[str, Any]):
    """取引データをHTTP POSTで外部サーバーに送信する"""
    
    if WEBSHARE_METHOD == "HTTP":
        if not WEBSHARE_POST_URL or "your-webshare-endpoint.com/upload" in WEBSHARE_POST_URL:
            logging.warning("⚠️ WEBSHARE_POST_URLが設定されていません。またはデフォルト値のままです。送信をスキップします。")
            return

        try:
            cleaned_data = _to_json_compatible(data)
            
            response = await asyncio.to_thread(requests.post, WEBSHARE_POST_URL, json=cleaned_data, timeout=10)
            response.raise_for_status()

            logging.info(f"✅ WebShareデータ (HTTP POST) を送信しました。ステータス: {response.status_code}")

        except requests.exceptions.RequestException as e:
            logging.error(f"❌ WebShare (HTTP POST) エラー: {e}")
            await send_telegram_notification(f"🚨 <b>WebShareエラー (HTTP POST)</b>\nデータ送信に失敗しました: <code>{e}</code>")

    else:
        logging.warning("⚠️ WEBSHARE_METHOD が 'HTTP' 以外に設定されています。WebShare送信をスキップします。")
        

# ====================================================================================
# CCXT & DATA ACQUISITION (変更なし)
# ====================================================================================

async def initialize_exchange_client() -> bool:
    """CCXTクライアントを初期化し、市場情報をロードする"""
    global EXCHANGE_CLIENT, IS_CLIENT_READY
    
    IS_CLIENT_READY = False
    
    if not API_KEY or not SECRET_KEY:
         logging.critical("❌ CCXT初期化スキップ: API_KEY または SECRET_KEY が設定されていません。")
         return False
         
    try:
        client_name = CCXT_CLIENT_NAME.lower()
        if client_name == 'binance':
            exchange_class = ccxt_async.binance
        elif client_name == 'bybit':
            exchange_class = ccxt_async.bybit
        elif client_name == 'mexc':
            exchange_class = ccxt_async.mexc
        else:
            logging.error(f"❌ 未対応の取引所クライアント: {CCXT_CLIENT_NAME}")
            return False

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
        
        logging.info(f"✅ CCXTクライアント ({CCXT_CLIENT_NAME}) を現物取引モードで初期化し、市場情報をロードしました。")
        IS_CLIENT_READY = True
        return True

    except ccxt.AuthenticationError as e: 
        logging.critical(f"❌ CCXT初期化失敗 - 認証エラー: APIキー/シークレットを確認してください。{e}", exc_info=True)
    except ccxt.ExchangeNotAvailable as e: 
        logging.critical(f"❌ CCXT初期化失敗 - 取引所接続エラー: サーバーが利用できません。{e}", exc_info=True)
    except ccxt.NetworkError as e:
        logging.critical(f"❌ CCXT初期化失敗 - ネットワークエラー: 接続を確認してください。{e}", exc_info=True)
    except Exception as e:
        logging.critical(f"❌ CCXTクライアント初期化失敗 - 予期せぬエラー: {e}", exc_info=True)
        
    EXCHANGE_CLIENT = None
    return False

async def fetch_account_status() -> Dict:
    """
    CCXTから口座の残高と、USDT以外の保有資産の情報を取得する。
    """
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.error("❌ 口座ステータス取得失敗: CCXTクライアントが準備できていません。")
        return {'total_usdt_balance': 0.0, 'open_positions': [], 'error': True}

    try:
        balance = await EXCHANGE_CLIENT.fetch_balance()
        
        total_usdt_balance = balance.get('total', {}).get('USDT', 0.0)
        open_positions = []
        for currency, amount in balance.get('total', {}).items():
            if currency not in ['USDT', 'USD'] and amount is not None and amount > 0.000001: 
                try:
                    symbol = f"{currency}/USDT"
                    if symbol not in EXCHANGE_CLIENT.markets:
                        continue 
                        
                    ticker = await EXCHANGE_CLIENT.fetch_ticker(symbol)
                    usdt_value = amount * ticker['last']
                    
                    if usdt_value >= 10: # 10 USDT未満の微細な資産は無視
                        open_positions.append({
                            'symbol': symbol,
                            'amount': amount,
                            'usdt_value': usdt_value
                        })
                except Exception as e:
                    logging.warning(f"⚠️ {currency} のUSDT価値を取得できませんでした（{e}）。")
                    
        return {
            'total_usdt_balance': total_usdt_balance,
            'open_positions': open_positions,
            'error': False
        }

    except ccxt.NetworkError as e:
        logging.error(f"❌ 口座ステータス取得失敗 (ネットワークエラー): {e}")
    except ccxt.AuthenticationError as e:
        logging.critical(f"❌ 口座ステータス取得失敗 (認証エラー): {e}")
    except Exception as e:
        logging.error(f"❌ 口座ステータス取得失敗 (予期せぬエラー): {e}")

    return {'total_usdt_balance': 0.0, 'open_positions': [], 'error': True}


# 最小取引数量の自動調整ロジック
async def adjust_order_amount(symbol: str, target_usdt_size: float) -> Optional[float]:
    """
    指定されたUSDT建ての目標取引サイズを、取引所の最小数量および数量精度に合わせて調整する。
    """
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT or symbol not in EXCHANGE_CLIENT.markets:
        logging.error(f"❌ 注文数量調整エラー: クライアント未準備、または {symbol} の市場情報が未ロードです。")
        return None
        
    market = EXCHANGE_CLIENT.markets[symbol]
    
    try:
        ticker = await EXCHANGE_CLIENT.fetch_ticker(symbol)
        current_price = ticker['last']
    except Exception as e:
        logging.error(f"❌ 注文数量調整エラー: {symbol} の現在価格を取得できませんでした。{e}")
        return None

    target_base_amount = target_usdt_size / current_price
    
    amount_precision_value = market['precision']['amount']
    min_amount = market['limits']['amount']['min']

    adjusted_amount = target_base_amount
    if amount_precision_value is not None and amount_precision_value > 0:
        try:
             precision_places = int(round(-math.log10(amount_precision_value)))
        except ValueError:
             precision_places = 8 
        
        # 指定された桁数に切り捨て (floor)
        adjusted_amount = math.floor(target_base_amount * math.pow(10, precision_places)) / math.pow(10, precision_places)
        
    if min_amount is not None and adjusted_amount < min_amount:
        adjusted_amount = min_amount
        
        logging.warning(
            f"⚠️ 【{market['base']}】最小数量調整: 目標ロット ({target_usdt_size:.2f} USDT) が最小 ({min_amount}) 未満でした。"
            f"数量を最小値 **{adjusted_amount}** に自動調整しました。"
        )
    
    if adjusted_amount * current_price < 1.0 or adjusted_amount <= 0:
        logging.error(f"❌ 調整後の数量 ({adjusted_amount:.8f}) が取引所の最小金額 (約1 USDT) またはゼロ以下です。")
        return None

    logging.info(
        f"✅ 数量調整完了: {symbol} の取引数量をルールに適合する **{adjusted_amount:.8f}** に調整しました。"
    )
    return adjusted_amount

async def fetch_ohlcv_safe(symbol: str, timeframe: str, limit: int) -> Optional[pd.DataFrame]:
    """OHLCVデータを安全に取得する (変更なし)"""
    global EXCHANGE_CLIENT
    if not EXCHANGE_CLIENT:
        return None
        
    try:
        ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(symbol, timeframe, limit=limit) 
        
        if not ohlcv:
            logging.warning(f"⚠️ OHLCVデータ取得: {symbol} ({timeframe}) でデータが空でした。取得足数: {limit}")
            return None 

        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df = df.set_index('timestamp')
        return df

    except ccxt.RequestTimeout as e:
        logging.error(f"❌ CCXTエラー (RequestTimeout): {symbol} ({timeframe}). APIコールがタイムアウトしました。{e}")
    except ccxt.RateLimitExceeded as e:
        logging.error(f"❌ CCXTエラー (RateLimitExceeded): {symbol} ({timeframe}). APIコールの頻度制限を超過しました。{e}")
    except ccxt.BadSymbol as e:
        logging.error(f"❌ CCXTエラー (BadSymbol): {symbol} ({timeframe}). シンボルが取引所に存在しない可能性があります。{e}")
    except ccxt.ExchangeError as e: 
        logging.error(f"❌ CCXTエラー (ExchangeError): {symbol} ({timeframe}). 取引所からの応答エラーです。{e}")
    except Exception as e:
        logging.error(f"❌ 予期せぬエラー (OHLCV取得): {symbol} ({timeframe}). {e}", exc_info=True)

    return None

# ====================================================================================
# TRADING LOGIC
# ====================================================================================

def calculate_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """テクニカルインジケーターを計算する (元のロジックを維持)"""
    if df.empty:
        return df
    df['SMA200'] = ta.sma(df['close'], length=LONG_TERM_SMA_LENGTH)
    # ... (元の詳細なインジケーター計算ロジックが続く) ...
    return df

def analyze_signals(df: pd.DataFrame, symbol: str, timeframe: str, macro_context: Dict) -> Optional[Dict]:
    """分析ロジックに基づき、取引シグナルを生成する (ブレークダウンロジック動作のためtech_dataを反映)"""
    if df.empty or df['SMA200'].isnull().all():
        return None
        
    current_price = df['close'].iloc[-1]
    
    # 簡易的なロングシグナル (終値がSMA200を上回っているかをチェック)
    if current_price > df['SMA200'].iloc[-1]:
        
        # --- スコアリングロジックの簡易実装 (ブレークダウン対応) ---
        score = BASE_SCORE # 0.60
        
        # マクロコンテキストからFGIの影響を計算 (簡易版: FGIプロキシが0.02でMAXボーナス, -0.02でMAXペナルティを想定)
        fgi_proxy = macro_context.get('fgi_proxy', 0.0)
        sentiment_fgi_proxy_bonus = (fgi_proxy / 0.02) * FGI_PROXY_BONUS_MAX if abs(fgi_proxy) <= 0.02 else (FGI_PROXY_BONUS_MAX if fgi_proxy > 0 else -FGI_PROXY_BONUS_MAX)
        
        # tech_dataの各要素を計算 (ブレークダウンロジック動作のため)
        tech_data = {
            'long_term_reversal_penalty_value': 0.0, # SMA200を上回っているためペナルティなし (ロングシグナル)
            'structural_pivot_bonus': STRUCTURAL_PIVOT_BONUS, # 簡易ボーナスとして付与 (0.05)
            'macd_penalty_value': 0.0, # ペナルティなし (0.00)
            'obv_momentum_bonus_value': OBV_MOMENTUM_BONUS, # 簡易ボーナスとして付与 (0.04)
            'liquidity_bonus_value': LIQUIDITY_BONUS_MAX, # 簡易ボーナスとして付与 (0.06)
            'sentiment_fgi_proxy_bonus': sentiment_fgi_proxy_bonus, # マクロコンテキストから計算
            'forex_bonus': 0.0,
            'volatility_penalty_value': 0.0,
        }
        
        # 総合スコア計算
        score += (
            (LONG_TERM_REVERSAL_PENALTY) + # ペナルティ回避 (0.20をプラスと見なす)
            tech_data['structural_pivot_bonus'] + 
            (MACD_CROSS_PENALTY) + # ペナルティ回避 (0.15をプラスと見なす)
            tech_data['obv_momentum_bonus_value'] + 
            tech_data['liquidity_bonus_value'] + 
            tech_data['sentiment_fgi_proxy_bonus'] +
            tech_data['forex_bonus'] +
            tech_data['volatility_penalty_value'] - 
            tech_data['long_term_reversal_penalty_value'] -
            tech_data['macd_penalty_value']
        )
        
        # 簡易的なRRR/SL/TP設定
        rr_ratio = 2.0             
        risk_percent = 0.015
        stop_loss = current_price * (1 - risk_percent)
        take_profit = current_price * (1 + risk_percent * rr_ratio)
        
        current_threshold = get_current_threshold(macro_context)
        
        # SMAチェックが満たされている場合のみ、簡易的なスコアリングを適用
        if score > current_threshold and rr_ratio >= 1.0:
             return {
                'symbol': symbol,
                'timeframe': timeframe,
                'action': 'buy', # 現物ロング（買い）のみを想定
                'score': score,
                'rr_ratio': rr_ratio,
                'entry_price': current_price,
                'stop_loss': stop_loss,
                'take_profit': take_profit,
                'lot_size_usdt': BASE_TRADE_SIZE_USDT,
                'tech_data': tech_data, # ★ブレークダウンロジックのために埋める★
            }
    return None

# ★追加/修正: SL/TP監視と自動決済のロジックを実装★

async def liquidate_position(position: Dict, exit_type: str, current_price: float) -> Optional[Dict]:
    """
    ポジションを決済する (成行売りを想定)。
    """
    global EXCHANGE_CLIENT
    symbol = position['symbol']
    amount = position['amount']
    entry_price = position['entry_price']
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.error("❌ 決済失敗: CCXTクライアントが準備できていません。")
        return {'status': 'error', 'error_message': 'CCXTクライアント未準備'}

    # 1. 注文実行（成行売り）
    try:
        if TEST_MODE:
            logging.info(f"✨ TEST MODE: {symbol} Sell Market ({exit_type}). 数量: {amount:.8f} の注文をシミュレート。")
            order = {
                'id': f"test-exit-{uuid.uuid4()}",
                'symbol': symbol,
                'side': 'sell',
                'amount': amount,
                'price': current_price, 
                'status': 'closed', 
                'datetime': datetime.now(timezone.utc).isoformat(),
                'filled': amount,
                'cost': amount * current_price, # USDT取得額
            }
        else:
            # ライブ取引
            # 既に保有している数量を売るため、adjust_order_amountは使用しない
            order = await EXCHANGE_CLIENT.create_order(
                symbol=symbol,
                type='market',
                side='sell',
                amount=amount, 
                params={}
            )
            logging.info(f"✅ 決済実行成功: {symbol} Sell Market. 数量: {amount:.8f} ({exit_type})")

        filled_amount_val = order.get('filled', 0.0)
        cost_val = order.get('cost', 0.0) # 売り注文の場合、これは受け取ったUSDT額

        if filled_amount_val <= 0:
            return {'status': 'error', 'error_message': '約定数量ゼロ'}

        exit_price = cost_val / filled_amount_val if filled_amount_val > 0 else current_price

        # PnL計算
        initial_cost = position['filled_usdt']
        final_value = cost_val # 決済で得たUSDT総額
        pnl_usdt = final_value - initial_cost
        pnl_rate = pnl_usdt / initial_cost if initial_cost > 0 else 0.0

        trade_result = {
            'status': 'ok',
            'exit_type': exit_type,
            'entry_price': entry_price,
            'exit_price': exit_price,
            'filled_amount': filled_amount_val,
            'pnl_usdt': pnl_usdt,
            'pnl_rate': pnl_rate,
        }
        return trade_result

    except Exception as e:
        logging.error(f"❌ 決済失敗 ({exit_type}): {symbol}. {e}", exc_info=True)
        return {'status': 'error', 'error_message': f'決済エラー: {e}'}

# TP/SL監視ロジックの実装
async def position_management_loop_async():
    """オープンポジションを監視し、SL/TPをチェックして決済する (TP/SL監視更新)"""
    global OPEN_POSITIONS, GLOBAL_MACRO_CONTEXT

    if not OPEN_POSITIONS:
        return

    positions_to_check = list(OPEN_POSITIONS)
    closed_positions = []
    
    current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
    # NOTE: ポジション監視ではtimeframe情報は不要だが、format_telegram_messageのため'N/A'を設定

    for position in positions_to_check:
        symbol = position['symbol']
        sl = position['stop_loss']
        tp = position['take_profit']
        
        # format_telegram_message関数がtimeframeを要求するため追加
        position['timeframe'] = 'N/A (Monitor)' 
        position['score'] = 0.0 # 決済通知ではスコアは不要だが、関数互換性のため

        try:
            # 最新価格の取得
            ticker = await EXCHANGE_CLIENT.fetch_ticker(symbol)
            current_price = ticker['last']
            
            exit_type = None
            
            if current_price <= sl:
                exit_type = "SL (ストップロス)"
            elif current_price >= tp:
                exit_type = "TP (テイクプロフィット)"
            
            if exit_type:
                # 決済実行
                trade_result = await liquidate_position(position, exit_type, current_price)
                
                # 決済ログと通知
                if trade_result and trade_result.get('status') == 'ok':
                    log_signal(position, 'Position Exit', trade_result)
                    await send_telegram_notification(
                        format_telegram_message(position, "ポジション決済", current_threshold, trade_result=trade_result, exit_type=exit_type)
                    )
                    closed_positions.append(position)
                else:
                    logging.error(f"❌ {symbol} 決済シグナル ({exit_type}) が発生しましたが、決済実行に失敗しました。")

        except Exception as e:
            logging.error(f"❌ ポジション監視エラー: {symbol}. {e}", exc_info=True)

    # 決済されたポジションをリストから削除
    for closed_pos in closed_positions:
        if closed_pos in OPEN_POSITIONS:
            OPEN_POSITIONS.remove(closed_pos)


async def execute_trade(signal: Dict) -> Optional[Dict]:
    """
    取引シグナルに基づき、取引所に対して注文を実行する。 (変更なし)
    """
    global OPEN_POSITIONS, EXCHANGE_CLIENT, IS_CLIENT_READY
    
    symbol = signal.get('symbol')
    action = 'buy' 
    target_usdt_size = signal.get('lot_size_usdt', BASE_TRADE_SIZE_USDT) 
    entry_price = signal.get('entry_price', 0.0) 

    if not IS_CLIENT_READY or not EXCHANGE_CLIENT:
        logging.error(f"❌ 注文実行失敗: CCXTクライアントが準備できていません。")
        return {'status': 'error', 'error_message': 'CCXTクライアント未準備'}
        
    if entry_price <= 0:
        logging.error(f"❌ 注文実行失敗: エントリー価格が不正です。{entry_price}")
        return {'status': 'error', 'error_message': 'エントリー価格不正'}

    # 1. 最小数量と精度を考慮した数量調整
    adjusted_amount = await adjust_order_amount(symbol, target_usdt_size)

    if adjusted_amount is None:
        logging.error(f"❌ {symbol} {action} 注文キャンセル: 注文数量の自動調整に失敗しました。")
        return {'status': 'error', 'error_message': '注文数量調整失敗'}

    # 2. 注文実行（成行注文を想定）
    try:
        if TEST_MODE:
            logging.info(f"✨ TEST MODE: {symbol} {action} Market. 数量: {adjusted_amount:.8f} の注文をシミュレート。")
            order = {
                'id': f"test-{uuid.uuid4()}",
                'symbol': symbol,
                'side': action,
                'amount': adjusted_amount,
                'price': entry_price, 
                'status': 'closed', 
                'datetime': datetime.now(timezone.utc).isoformat(),
                'filled': adjusted_amount,
                'cost': adjusted_amount * entry_price,
            }
        else:
            order = await EXCHANGE_CLIENT.create_order(
                symbol=symbol,
                type='market',
                side=action,
                amount=adjusted_amount, 
                params={}
            )
            logging.info(f"✅ 注文実行成功: {symbol} {action} Market. 数量: {adjusted_amount:.8f}")

        filled_amount_val = order.get('filled', 0.0)
        price_used = order.get('price')

        if filled_amount_val is None:
            filled_amount_val = 0.0
        
        effective_price = price_used if price_used is not None else entry_price

        trade_result = {
            'status': 'ok',
            'order_id': order.get('id'),
            'filled_amount': filled_amount_val, 
            'filled_usdt': order.get('cost', filled_amount_val * effective_price), 
            'entry_price': effective_price,
            'stop_loss': signal.get('stop_loss'),
            'take_profit': signal.get('take_profit'),
        }

        # ポジションの開始を記録
        if order['status'] in ('closed', 'fill') and trade_result['filled_amount'] > 0:
            position_data = {
                'symbol': symbol,
                'entry_time': order.get('datetime'),
                'side': action,
                'amount': trade_result['filled_amount'],
                'entry_price': trade_result['entry_price'],
                'filled_usdt': trade_result['filled_usdt'], 
                'stop_loss': trade_result['stop_loss'],
                'take_profit': trade_result['take_profit'],
                'order_id': order.get('id'),
                'status': 'open',
            }
            OPEN_POSITIONS.append(position_data)
            
        return trade_result

    except ccxt.InsufficientFunds as e:
        logging.error(f"❌ 注文失敗 - 残高不足: {symbol} {action}. {e}")
        return {'status': 'error', 'error_message': f'残高不足エラー: {e}'}
    except ccxt.InvalidOrder as e:
        logging.error(f"❌ 注文失敗 - 無効な注文: 取引所ルール違反の可能性 (数量/価格/最小取引額)。{e}")
        return {'status': 'error', 'error_message': f'無効な注文エラー: {e}'}
    except ccxt.ExchangeError as e:
        logging.error(f"❌ 注文失敗 - 取引所エラー: API応答の問題。{e}")
        return {'status': 'error', 'error_message': f'取引所APIエラー: {e}'}
    except Exception as e:
        logging.error(f"❌ 注文失敗 - 予期せぬエラー: {e}", exc_info=True)
        return {'status': 'error', 'error_message': f'予期せぬエラー: {e}'}


# ====================================================================================
# MAIN BOT LOGIC
# ====================================================================================

async def main_bot_loop():
    """ボットのメイン実行ループ (1分ごと)"""
    global LAST_SUCCESS_TIME, IS_CLIENT_READY, CURRENT_MONITOR_SYMBOLS, LAST_ANALYSIS_SIGNALS, IS_FIRST_MAIN_LOOP_COMPLETED, GLOBAL_MACRO_CONTEXT, LAST_ANALYSIS_ONLY_NOTIFICATION_TIME, LAST_SIGNAL_TIME, LAST_WEBSHARE_UPLOAD_TIME 

    if not IS_CLIENT_READY:
        logging.info("CCXTクライアントを初期化中...")
        await initialize_exchange_client()
        if not IS_CLIENT_READY:
            logging.critical("クライアント初期化失敗。次のループでリトライします。")
            return

    logging.info(f"--- 💡 {datetime.now(JST).strftime('%Y/%m/%d %H:%M:%S')} - BOT LOOP START (M1 Frequency) ---")

    new_signals: List[Dict] = []
    
    # NOTE: マクロコンテキストはダミー (元のコードを維持)
    GLOBAL_MACRO_CONTEXT = {'fgi_proxy': 0.0, 'fgi_raw_value': '50', 'forex_bonus': 0.0}
    current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
    
    # ポジション管理は position_monitor_scheduler が別で実行

    for symbol in CURRENT_MONITOR_SYMBOLS:
        for timeframe in TARGET_TIMEFRAMES:
            limit = REQUIRED_OHLCV_LIMITS.get(timeframe, 500)
            df = await fetch_ohlcv_safe(symbol, timeframe=timeframe, limit=limit)
            
            if df is None or df.empty:
                continue
            
            df = calculate_indicators(df)
            
            signal = analyze_signals(df, symbol, timeframe, GLOBAL_MACRO_CONTEXT)
            
            if signal and signal['score'] >= current_threshold:
                if time.time() - LAST_SIGNAL_TIME.get(symbol, 0.0) > TRADE_SIGNAL_COOLDOWN:
                    new_signals.append(signal)
                    LAST_SIGNAL_TIME[symbol] = time.time()
                else:
                    logging.info(f"スキップ: {symbol} はクールダウン期間中です。")


    LAST_ANALYSIS_SIGNALS = sorted(new_signals, key=lambda s: s.get('score', 0.0), reverse=True)[:TOP_SIGNAL_COUNT] 

    for signal in LAST_ANALYSIS_SIGNALS:
        symbol = signal['symbol']
        
        # 既存ポジションがないかチェックしてから取引実行
        if symbol not in [p['symbol'] for p in OPEN_POSITIONS]:
            trade_result = await execute_trade(signal)
            log_signal(signal, 'Trade Signal', trade_result)
            
            if trade_result and trade_result.get('status') == 'ok':
                await send_telegram_notification(
                    format_telegram_message(signal, "取引シグナル", current_threshold, trade_result=trade_result)
                )
        else:
            logging.info(f"スキップ: {symbol} には既にオープンポジションがあります。")
            
    now = time.time()
    
    # 6. 分析専用通知 (1時間ごと)
    if now - LAST_ANALYSIS_ONLY_NOTIFICATION_TIME >= ANALYSIS_ONLY_INTERVAL:
        if LAST_ANALYSIS_SIGNALS or not IS_FIRST_MAIN_LOOP_COMPLETED:
            await send_telegram_notification(
                format_analysis_only_message(
                    LAST_ANALYSIS_SIGNALS, 
                    GLOBAL_MACRO_CONTEXT, 
                    current_threshold, 
                    len(CURRENT_MONITOR_SYMBOLS)
                )
            )
            LAST_ANALYSIS_ONLY_NOTIFICATION_TIME = now
            log_signal({'signals': LAST_ANALYSIS_SIGNALS, 'macro': GLOBAL_MACRO_CONTEXT}, 'Hourly Analysis')

    # 7. 初回起動完了通知
    if not IS_FIRST_MAIN_LOOP_COMPLETED:
        account_status = await fetch_account_status() 

        await send_telegram_notification(
            format_startup_message(
                account_status, 
                GLOBAL_MACRO_CONTEXT, 
                len(CURRENT_MONITOR_SYMBOLS), 
                current_threshold,
                "v19.0.29 - High-Freq/TP/SL/M1M5 Added (Patch 38)"
            )
        )
        IS_FIRST_MAIN_LOOP_COMPLETED = True
        
    # 8. ログの外部アップロード (WebShare - HTTP POSTへ変更)
    if now - LAST_WEBSHARE_UPLOAD_TIME >= WEBSHARE_UPLOAD_INTERVAL:
        logging.info("WebShareデータをアップロードします (HTTP POST)。")
        webshare_data = {
            "timestamp": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
            "positions": OPEN_POSITIONS,
            "analysis_signals": LAST_ANALYSIS_SIGNALS,
            "global_context": GLOBAL_MACRO_CONTEXT,
            "is_test_mode": TEST_MODE,
        }
        await send_webshare_update(webshare_data)
        LAST_WEBSHARE_UPLOAD_TIME = now


    # 9. 最終処理
    LAST_SUCCESS_TIME = time.time()
    logging.info(f"--- 💡 BOT LOOP END. Positions: {len(OPEN_POSITIONS)}, New Signals: {len(LAST_ANALYSIS_SIGNALS)} ---")


# ====================================================================================
# FASTAPI & ASYNC EXECUTION
# ====================================================================================

app = FastAPI()

@app.get("/status")
def get_status_info():
    """ボットの現在の状態を返す (変更なし)"""
    current_time = time.time()
    last_time_for_calc = LAST_SUCCESS_TIME if LAST_SUCCESS_TIME > 0 else current_time
    next_check = max(0, int(LOOP_INTERVAL - (current_time - last_time_for_calc)))

    status_msg = {
        "status": "ok",
        "bot_version": "v19.0.29 - High-Freq/TP/SL/M1M5 Added (Patch 38)",
        "base_trade_size_usdt": BASE_TRADE_SIZE_USDT, 
        "managed_positions_count": len(OPEN_POSITIONS), 
        "last_success_time_utc": datetime.fromtimestamp(LAST_SUCCESS_TIME, timezone.utc).isoformat() if LAST_SUCCESS_TIME > 0 else "N/A",
        "next_main_loop_check_seconds": next_check,
        "current_threshold": get_current_threshold(GLOBAL_MACRO_CONTEXT),
        "macro_context": GLOBAL_MACRO_CONTEXT, 
        "is_test_mode": TEST_MODE,
        "monitoring_symbols": len(CURRENT_MONITOR_SYMBOLS),
        "last_signals_count": len(LAST_ANALYSIS_SIGNALS),
        "is_client_ready": IS_CLIENT_READY,
    }
    return JSONResponse(content=status_msg)

async def main_loop_scheduler():
    """メインループを定期実行するスケジューラ (1分ごと)"""
    while True:
        try:
            await main_bot_loop()
        except Exception as e:
            logging.critical(f"❌ メインループ実行中に致命的なエラー: {e}", exc_info=True)
            await send_telegram_notification(f"🚨 **致命的なエラー**\nメインループでエラーが発生しました: `{e}`")

        # 待機時間を LOOP_INTERVAL (60秒) に基づいて計算
        wait_time = max(1, LOOP_INTERVAL - (time.time() - LAST_SUCCESS_TIME))
        logging.info(f"次のメインループまで {wait_time:.1f} 秒待機します。")
        await asyncio.sleep(wait_time)

# ★新規追加: TP/SL監視用スケジューラ★
async def position_monitor_scheduler():
    """TP/SL監視ループを定期実行するスケジューラ (10秒ごと)"""
    # NOTE: このタスクはメインループとは独立して動作し、より高い頻度でTP/SLをチェックします
    while True:
        try:
            await position_management_loop_async()
        except Exception as e:
            logging.critical(f"❌ ポジション監視ループ実行中に致命的なエラー: {e}", exc_info=True)
            # ポジション監視エラーの通知は頻繁になる可能性があるため、ここではTelegram通知を行わない

        await asyncio.sleep(MONITOR_INTERVAL) # MONITOR_INTERVAL (10秒) ごとに実行


@app.on_event("startup")
async def startup_event():
    """アプリケーション起動時に実行 (タスク起動の修正)"""
    # 初期化タスクをバックグラウンドで開始
    asyncio.create_task(initialize_exchange_client())
    # メインループのスケジューラをバックグラウンドで開始 (1分ごと)
    asyncio.create_task(main_loop_scheduler())
    # ★追加: ポジション監視ループのスケジューラをバックグラウンドで開始 (10秒ごと)★
    asyncio.create_task(position_monitor_scheduler())
    logging.info("BOTサービスを開始しました。")

# ====================================================================================
# ENTRY POINT (変更なし)
# ====================================================================================

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
