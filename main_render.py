# ====================================================================================
# Apex BOT v19.0.53 - FEATURE: Periodic SL/TP Re-Placing for Unmanaged Orders
#
# 改良・修正点:
# 1. 【SL/TP再設定】open_order_management_loop関数内に、SLまたはTPの注文が片方または両方欠けている場合に、
#    残っている注文をキャンセルし、SL/TP注文を再設定するロジックを追加。
# 2. 【IOC失敗診断維持】v19.0.52で追加したIOC失敗時診断ログを維持。
# 3. BOT_VERSION を v19.0.53 に更新。
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
import math 

# .envファイルから環境変数を読み込む
load_dotenv()

# 💡 【ログ確認対応】ロギング設定を明示的に定義
logging.basicConfig(
    level=logging.INFO, # INFOレベル以上のメッセージを出力
    format='%(asctime)s - %(levelname)s - (%(funcName)s) - (%(threadName)s) - %(message)s' 
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
    "FLOW/USDT", "IMX/USDT", "SUI/USDT", "ASTER/USDT", "ENA/USDT",
    "ZEC/USDT", "PUMP/USDT", "PEPE/USDT", "FARTCOIN/USDT",
    "WLFI/USDT", "PENGU/USDT", "ONDO/USDT", "HBAR/USDT", "TRUMP/USDT",
    "SHIB/USDT", "HYPE/USDT", "LINK/USDT", "ZEC/USDT",
    "VIRTUAL/USDT", "PIPPIN/USDT", "GIGGLE/USDT", "H/USDT", "AIXBT/USDT", 
]
TOP_SYMBOL_LIMIT = 40               # 監視対象銘柄の最大数 (出来高TOPから選出)
LOOP_INTERVAL = 60 * 1              # メインループの実行間隔 (秒) - 1分ごと
MONITOR_INTERVAL = 10               # オープン注文監視ループの実行間隔 (秒) - 10秒ごと
HOURLY_SCORE_REPORT_INTERVAL = 60 * 60 # ★ 1時間ごとのスコア通知間隔 (60分ごと)

# 💡 クライアント設定
CCXT_CLIENT_NAME = "mexc" # ★明示的にmexcに設定 (環境変数による上書きを無視)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
API_KEY = os.getenv(f"{CCXT_CLIENT_NAME.upper()}_API_KEY") # 環境変数 MEXC_API_KEY を参照
SECRET_KEY = os.getenv(f"{CCXT_CLIENT_NAME.upper()}_SECRET") # 環境変数 MEXC_SECRET を参照
TEST_MODE = os.getenv("TEST_MODE", "False").lower() in ('true', '1', 't')
SKIP_MARKET_UPDATE = os.getenv("SKIP_MARKET_UPDATE", "False").lower() in ('true', '1', 't')

# 💡 自動売買設定 (動的ロットのベースサイズ)
try:
    # 総資産額が不明な場合や、動的ロットの最小値として使用
    BASE_TRADE_SIZE_USDT = float(os.getenv("BASE_TRADE_SIZE_USDT", "100")) 
except ValueError:
    BASE_TRADE_SIZE_USDT = 100.0
    logging.warning("⚠️ BASE_TRADE_SIZE_USDTが不正な値です。100 USDTを使用します。")
    
if BASE_TRADE_SIZE_USDT < 10:
    logging.warning("⚠️ BASE_TRADE_SIZE_USDTが10 USDT未満です。ほとんどの取引所の最小取引額を満たさない可能性があります。")


# 【動的ロット設定】
DYNAMIC_LOT_MIN_PERCENT = 0.10 # 最小ロット (総資産の 10%)
DYNAMIC_LOT_MAX_PERCENT = 0.20 # 最大ロット (総資産の 20%)

# 💡 新規取引制限設定 【★V19.0.33で追加】
MIN_USDT_BALANCE_FOR_TRADE = 20.0 # 新規取引に必要な最小USDT残高 (20.0 USDT)
DYNAMIC_LOT_SCORE_MAX = 0.96   # このスコアで最大ロットが適用される (96点)


# グローバル変数 (状態管理用)
EXCHANGE_CLIENT: Optional[ccxt_async.Exchange] = None
CURRENT_MONITOR_SYMBOLS: List[str] = DEFAULT_SYMBOLS.copy()
LAST_SUCCESS_TIME: float = 0.0
LAST_SIGNAL_TIME: Dict[str, float] = {}
LAST_ANALYSIS_SIGNALS: List[Dict] = []
LAST_HOURLY_NOTIFICATION_TIME: float = 0.0 # ★ 1時間ごとの通知時刻
GLOBAL_MACRO_CONTEXT: Dict = {'fgi_proxy': 0.0, 'fgi_raw_value': 'N/A', 'forex_bonus': 0.0} # ★初期値を設定
IS_FIRST_MAIN_LOOP_COMPLETED: bool = False # 初回メインループ完了フラグ
OPEN_POSITIONS: List[Dict] = [] # 現在保有中のポジション (注文IDトラッキング用)
GLOBAL_TOTAL_EQUITY: float = 0.0 # 総資産額を格納するグローバル変数
HOURLY_SIGNAL_LOG: List[Dict] = [] # ★ 1時間内のシグナルを一時的に保持するリスト (V19.0.34で追加)
HOURLY_ATTEMPT_LOG: Dict[str, str] = {} # ★ 1時間内の分析試行を保持するリスト (Symbol: Reason)

# ★ 新規追加: ボットのバージョン (v19.0.53: 定期SL/TP再設定機能)
BOT_VERSION = "v19.0.53"

if TEST_MODE:
    logging.warning("⚠️ WARNING: TEST_MODE is active. Trading is disabled.")

# CCXTクライアントの準備完了フラグ
IS_CLIENT_READY: bool = False

# 取引ルール設定
TRADE_SIGNAL_COOLDOWN = 60 * 60 * 2 # 同一銘柄のシグナル通知クールダウン（2時間）
SIGNAL_THRESHOLD = 0.65             # 動的閾値のベースライン（使われていないが定数として残す）
TOP_SIGNAL_COUNT = 3                # 通知するシグナルの最大数
REQUIRED_OHLCV_LIMITS = {'1m': 500, '5m': 500, '15m': 500, '1h': 500, '4h': 500} # 1m, 5mを含む

# ====================================================================================
# 【★スコアリング定数変更 V19.0.33: 最大スコア100点に正規化 (要件4)】
# (合計最大スコアが1.00になるように調整)
# ====================================================================================
TARGET_TIMEFRAMES = ['1m', '5m', '15m', '1h', '4h'] 

# スコアリングウェイト
BASE_SCORE = 0.50                   # ベースとなる取引基準点 (50点)
LONG_TERM_SMA_LENGTH = 200          # 長期トレンドフィルタ用SMA

# ペナルティ（マイナス要因）
LONG_TERM_REVERSAL_PENALTY = 0.30   # 長期トレンド逆行時のペナルティを強化
MACD_CROSS_PENALTY = 0.25           # MACDが不利なクロス/発散時のペナルティを強化
VOLATILITY_BB_PENALTY_THRESHOLD = 0.01 # BB幅が1%未満

# ボーナス（プラス要因）- 合計0.50点に調整
TREND_ALIGNMENT_BONUS = 0.10        # 中期/長期トレンド一致時のボーナス (元: 0.15)
STRUCTURAL_PIVOT_BONUS = 0.06       # 価格構造/ピボット支持時のボーナス (元: 0.10)
RSI_MOMENTUM_LOW = 45               # RSIが45以下でロングモメンタム候補
RSI_MOMENTUM_BONUS_MAX = 0.10       # RSIの強さに応じた可変ボーナスの最大値 (元: 0.15)
OBV_MOMENTUM_BONUS = 0.05           # OBVの確証ボーナス (元: 0.08)
VOLUME_INCREASE_BONUS = 0.07        # 出来高スパイク時のボーナス (元: 0.10)
LIQUIDITY_BONUS_MAX = 0.07          # 流動性(板の厚み)による最大ボーナス (元: 0.10)
FGI_PROXY_BONUS_MAX = 0.05          # 恐怖・貪欲指数による最大ボーナス/ペナルティ (変更なし)

# 市場環境に応じた動的閾値調整のための定数 (★ V19.0.51 修正箇所: 閾値を86点ベースに設定)
FGI_SLUMP_THRESHOLD = -0.02         
FGI_ACTIVE_THRESHOLD = 0.02         
SIGNAL_THRESHOLD_SLUMP = 0.88       # 88.00点 (リスクオフ時は厳しく)
SIGNAL_THRESHOLD_NORMAL = 0.86      # 86.00点 (ベースライン)
SIGNAL_THRESHOLD_ACTIVE = 0.83      # 83.00点 (リスクオン時は緩く)      

# ====================================================================================
# UTILITIES & FORMATTING 
# ====================================================================================

def format_usdt(amount: float) -> str:
    """USDT金額（ロットサイズ、PnLなど）を整形する"""
    if amount is None:
        amount = 0.0
        
    if amount >= 1.0:
        return f"{amount:,.2f}"
    elif amount >= 0.01:
        return f"{amount:.4f}"
    else:
        return f"{amount:.6f}"

def format_price_precision(price: float) -> str:
    """価格を整形する。1.0 USDT以上の価格に対して小数第4位まで表示を保証する。【★V19.0.32で追加】"""
    if price is None:
        price = 0.0
        
    if price >= 1.0:
        # 1.0 USDT以上の価格は小数第4位まで表示を保証
        return f"{price:,.4f}"
    elif price >= 0.01:
        # 0.01 USDT以上1.0 USDT未満は小数第4位
        return f"{price:.4f}"
    else:
        # 0.01 USDT未満は小数第6位 (精度維持)
        return f"{price:.6f}"

# 💡 【★V19.0.41 修正箇所】 スコアに基づいて推定勝率を返す関数 (より細かく、最低勝率0%対応)
def get_estimated_win_rate(score: float) -> str:
    """スコアに基づいて推定勝率を返す (より細かい段階と最低勝率0%に対応)"""
    
    # 1. スコアと勝率の基準点を設定
    # 0.60点 (60点) を勝率 0% のベースラインとする
    min_score = 0.60
    max_score = 1.00
    min_win_rate = 0.0 # 0%
    max_win_rate = 95.0 # 95%
    
    # 2. スコアを勝率パーセンテージに変換 (線形近似を使用)
    if score <= min_score:
        base_rate = 0.0
    elif score >= max_score:
        base_rate = max_win_rate # 95.0
    else:
        # 線形補間: V = V_min + (V_max - V_min) * ((S - S_min) / (S_max - S_min))
        ratio = (score - min_score) / (max_score - min_score)
        base_rate = min_win_rate + (max_win_rate - min_win_rate) * ratio
            
    # 3. 段階に分割して表示 (より細かい粒度 5%刻み)
    if base_rate >= 95:
        return "95%+"
    elif base_rate >= 90:
        return "90-95%"
    elif base_rate >= 85:
        return "85-90%"
    elif base_rate >= 80:
        return "80-85%"
    elif base_rate >= 75:
        return "75-80%"
    elif base_rate >= 70:
        return "70-75%"
    elif base_rate >= 65:
        return "65-70%"
    elif base_rate >= 60:
        return "60-65%"
    elif base_rate >= 50:
        return "50-60%"
    elif base_rate >= 30:
        return "30-50%"
    elif base_rate > 0:
        # 0%より大きく、30%未満
        return "1-30%"
    else:
        return "0%"

def get_current_threshold(macro_context: Dict) -> float:
    """FGI proxyに基づいて現在の取引閾値を動的に決定する"""
    fgi_proxy = macro_context.get('fgi_proxy', 0.0)
    
    if fgi_proxy > FGI_ACTIVE_THRESHOLD:
        return SIGNAL_THRESHOLD_ACTIVE
    elif fgi_proxy < FGI_SLUMP_THRESHOLD:
        return SIGNAL_THRESHOLD_SLUMP
    else:
        return SIGNAL_THRESHOLD_NORMAL

def get_score_breakdown(signal: Dict) -> str:
    """シグナルに含まれるテクニカルデータから、スコアの詳細なブレークダウンを文字列として返す"""
    tech_data = signal.get('tech_data', {})
    score = signal['score']
    
    breakdown = []
    
    # ベーススコア
    base_score_line = f"  - **ベーススコア ({signal['timeframe']})**: <code>+{BASE_SCORE*100:.1f}</code> 点"
    breakdown.append(base_score_line)
    
    # 長期トレンド逆行ペナルティ
    lt_reversal_pen = tech_data.get('long_term_reversal_penalty_value', 0.0)
    lt_status = '❌ 長期トレンド逆行' if lt_reversal_pen > 0 else '✅ 長期トレンド一致'
    lt_score = f"{(-lt_reversal_pen)*100:.1f}"
    breakdown.append(f"  - {lt_status} (SMA200乖離): <code>{lt_score}</code> 点")
    
    # 中期トレンドアライメントボーナス
    trend_alignment_bonus = tech_data.get('trend_alignment_bonus_value', 0.0)
    trend_status = '✅ 中期/長期トレンド一致 (SMA50>200)' if trend_alignment_bonus > 0 else '➖ 中期トレンド 中立/逆行'
    trend_score = f"{trend_alignment_bonus*100:.1f}"
    breakdown.append(f"  - {trend_status}: <code>+{trend_score}</code> 点")
    
    # 価格構造/ピボット
    pivot_bonus = tech_data.get('structural_pivot_bonus', 0.0)
    pivot_status = '✅ 価格構造/ピボット支持' if pivot_bonus > 0 else '➖ 価格構造 中立'
    pivot_score = f"{pivot_bonus*100:.1f}"
    breakdown.append(f"  - {pivot_status}: <code>+{pivot_score}</code> 点")

    # MACDペナルティ
    macd_pen = tech_data.get('macd_penalty_value', 0.0)
    macd_status = '❌ MACDクロス/発散 (不利)' if macd_pen > 0 else '➖ MACD 中立'
    macd_score = f"{(-macd_pen)*100:.1f}"
    breakdown.append(f"  - {macd_status}: <code>{macd_score}</code> 点")

    # RSIモメンタムボーナス (可変)
    rsi_momentum_bonus = tech_data.get('rsi_momentum_bonus_value', 0.0)
    rsi_status = f"✅ RSIモメンタム加速 ({tech_data.get('rsi_value', 0.0):.1f})" if rsi_momentum_bonus > 0 else '➖ RSIモメンタム 中立'
    rsi_score = f"{rsi_momentum_bonus*100:.1f}"
    breakdown.append(f"  - {rsi_status}: <code>+{rsi_score}</code> 点")
    
    # 出来高/OBV確証ボーナス
    obv_bonus = tech_data.get('obv_momentum_bonus_value', 0.0)
    obv_status = '✅ 出来高/OBV確証' if obv_bonus > 0 else '➖ 出来高/OBV 中立'
    obv_score = f"{obv_bonus*100:.1f}"
    breakdown.append(f"  - {obv_status}: <code>+{obv_score}</code> 点")
    
    # 出来高スパイクボーナス
    volume_increase_bonus = tech_data.get('volume_increase_bonus_value', 0.0)
    volume_status = '✅ 直近の出来高スパイク' if volume_increase_bonus > 0 else '➖ 出来高スパイクなし'
    volume_score = f"{volume_increase_bonus*100:.1f}"
    breakdown.append(f"  - {volume_status}: <code>+{volume_score}</code> 点")

    # 流動性
    liquidity_bonus = tech_data.get('liquidity_bonus_value', 0.0)
    liquidity_status = '✅ 流動性 (板の厚み) 優位'
    liquidity_score = f"{liquidity_bonus*100:.1f}"
    breakdown.append(f"  - {liquidity_status}: <code>+{liquidity_score}</code> 点")

    # マクロ環境
    fgi_bonus = tech_data.get('sentiment_fgi_proxy_bonus', 0.0)
    macro_status = '✅ FGIマクロ影響 順行' if fgi_bonus >= 0 else '❌ FGIマクロ影響 逆行'
    macro_score = f"{fgi_bonus*100:.1f}"
    breakdown.append(f"  - {macro_status}: <code>{macro_score}</code> 点")

    # ボラティリティペナルティ (低ボラティリティ)
    volatility_pen = tech_data.get('volatility_penalty_value', 0.0)
    vol_status = '❌ 低ボラティリティペナルティ' if volatility_pen < 0 else '➖ ボラティリティ 中立'
    vol_score = f"{volatility_pen*100:.1f}"
    breakdown.append(f"  - {vol_status}: <code>{vol_score}</code> 点")

    return '\n'.join(breakdown)

def format_startup_message(
    account_status: Dict, 
    macro_context: Dict, 
    monitoring_count: int,
    current_threshold: float,
    bot_version: str # この引数はそのまま維持
) -> str:
    """初回起動完了通知用のメッセージを作成する"""
    now_jst = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")
    
    fgi_raw_value = macro_context.get('fgi_raw_value', 'N/A')
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
        f"  - **総資産額 (Equity)**: <code>{format_usdt(account_status['total_equity'])}</code> USDT\n" 
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
                sl_display = format_price_precision(pos['stop_loss'])
                tp_display = format_price_precision(pos['take_profit'])
                balance_section += f"    - Top {i+1}: {base_currency} (SL: {sl_display} / TP: {tp_display})\n"
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
        f"  - **FGI (恐怖・貪欲)**: <code>{fgi_raw_value}</code> ({'リスクオン' if fgi_proxy > FGI_ACTIVE_THRESHOLD else ('リスクオフ' if fgi_proxy < FGI_SLUMP_THRESHOLD else '中立')})\n"
        f"  - **総合マクロ影響**: <code>{((fgi_proxy + forex_bonus) * 100):.2f}</code> 点\n\n"
    )

    footer = (
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"<pre>※ この通知はメインの分析ループが一度完了したことを示します。指値とSL/TP注文は取引所側で管理されています。</pre>"
    )

    return header + balance_section + macro_section + footer


# ★ v19.0.46 修正点: exit_typeを分離し、bot_versionの代わりにグローバル定数 BOT_VERSION を使用するように修正
def format_telegram_message(signal: Dict, context: str, current_threshold: float, trade_result: Optional[Dict] = None, exit_type: Optional[str] = None) -> str:
    """Telegram通知用のメッセージを作成する"""
    global GLOBAL_TOTAL_EQUITY, BOT_VERSION
    
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
    failure_section = "" # 💡 取引失敗詳細セクションの追加

    if context == "取引シグナル":
        lot_size = signal.get('lot_size_usdt', BASE_TRADE_SIZE_USDT)
        
        # ロットサイズ割合の表示 (金額なのでformat_usdt)
        if GLOBAL_TOTAL_EQUITY > 0 and lot_size >= BASE_TRADE_SIZE_USDT:
            lot_percent = (lot_size / GLOBAL_TOTAL_EQUITY) * 100
            lot_info = f"<code>{format_usdt(lot_size)}</code> USDT ({lot_percent:.1f}%)"
        else:
            lot_info = f"<code>{format_usdt(lot_size)}</code> USDT"
        
        if TEST_MODE:
            trade_status_line = f"⚠️ **テストモード**: 取引は実行されません。(ロット: {lot_info})"
        
        elif trade_result is None or trade_result.get('status') == 'error':
            error_message = trade_result.get('error_message', 'APIエラー') if trade_result else 'システムエラー'
            
            # 💡 注文タイプを自動で判断して表示 (V19.0.51ではIOC指値注文を想定)
            if '指値買い注文' in error_message:
                 trade_status_line = f"❌ **自動売買 失敗**: 指値買い注文が即時約定しなかったためキャンセルされました。"
            elif '成行買い注文' in error_message:
                 trade_status_line = f"❌ **自動売買 失敗**: 成行買い注文で約定が発生しませんでした。"
            else:
                 trade_status_line = f"❌ **自動売買 失敗**: {error_message}"
            
            # 💡 取引失敗詳細セクションの生成
            # SL/TP設定失敗後の強制クローズの結果を詳細に表示する
            close_status = trade_result.get('close_status')
            
            failure_section_lines = [f"  - ❌ {error_message}"]
            
            if close_status == 'ok':
                 close_amount = trade_result.get('closed_amount', 0.0)
                 close_message = f"✅ 不完全ポジションを即時クローズしました (数量: {close_amount:.4f})。"
                 failure_section_lines.append(f"  - {close_message}")
            elif close_status == 'error':
                 close_error = trade_result.get('close_error_message', '不明なエラー')
                 close_message = f"🚨 不完全ポジションの強制クローズに失敗しました: {close_error}"
                 failure_section_lines.append(f"  - {close_message}")
                 failure_section_lines.append(f"  - **🚨 ポジションが残っている可能性があります。手動で確認・決済してください。**")
            elif close_status == 'skipped':
                 failure_section_lines.append(f"  - ➖ ポジションは約定しなかった、または約定数量がゼロのためクローズはスキップされました。")
            
            failure_section = (
                f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
                f"**取引失敗詳細**:\n"
                f"{'\n'.join(failure_section_lines)}\n"
            )

        elif trade_result.get('status') == 'ok':
            # ★ V19.0.45 修正: 注文タイプを明確化
            trade_status_line = "✅ **自動売買 成功**: 現物指値買い注文が即時約定しました。"
            
            filled_amount = trade_result.get('filled_amount', 0.0) 
            filled_usdt = trade_result.get('filled_usdt', 0.0)
            
            trade_section = (
                f"💰 **取引実行結果**\n"
                f"  - **注文タイプ**: <code>現物 (Spot) / 指値買い (IOC)</code>\n" # ★ IOCに変更
                f"  - **動的ロット**: {lot_info} (目標)\n"
                f"  - **約定数量**: <code>{filled_amount:.4f}</code> {symbol.split('/')[0]}\n"
                f"  - **平均約定額**: <code>{format_usdt(filled_usdt)}</code> USDT\n"
                f"  - **SL注文ID**: <code>{trade_result.get('sl_order_id', 'N/A')}</code>\n"
                f"  - **TP注文ID**: <code>{trade_result.get('tp_order_id', 'N/A')}</code>\n"
            )
            
    elif context == "ポジション決済":
        exit_type_final = trade_result.get('exit_type', exit_type or '不明')
        
        if exit_type_final == "SL":
            trade_status_line = f"🛑 **ストップロス (SL) 決済**"
        elif exit_type_final == "TP":
            trade_status_line = f"🎯 **テイクプロフィット (TP) 決済**"
        elif exit_type_final == "取引所決済完了":
            trade_status_line = f"🟢 **取引所決済完了 (SL/TP 約定確認)**"
        else:
            trade_status_line = f"⚪️ **ポジション決済 (Exit Type: {exit_type_final})**"
            
        pnl = trade_result.get('pnl', 0.0)
        pnl_percent = trade_result.get('pnl_percent', 0.0)
        
        pnl_sign = "🟢" if pnl >= 0 else "🔴"
        pnl_color = "green" if pnl >= 0 else "red"
        
        trade_section = (
            f"📈 **決済結果**\n"
            f"  - **エントリー価格**: <code>{format_price_precision(entry_price)}</code>\n"
            f"  - **決済価格**: <code>{format_price_precision(trade_result.get('close_price', 0.0))}</code>\n"
            f"  - **確定損益 ({pnl_sign})**: <b style=\"color:{pnl_color}\">{format_usdt(pnl)} USDT</b>\n"
            f"  - **損益率 ({pnl_sign})**: <b style=\"color:{pnl_color}\">{pnl_percent:.2f}%</b>\n"
            f"  - **SL/TP設定**: <code>{format_price_precision(stop_loss)}</code> / <code>{format_price_precision(take_profit)}</code>\n"
            f"  - **投入ロット (約定額)**: <code>{format_usdt(trade_result.get('filled_usdt', 0.0))}</code> USDT\n"
        )


    # メッセージの構築
    message = (
        f"{trade_status_line}\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"🚀 **{symbol}** ({timeframe}) - {now_jst} (JST)\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
    )

    if context == "取引シグナル":
        # シグナル情報セクション
        message += (
            f"**📊 シグナルスコア**: <code>{score*100:.2f} / 100</code> (閾値: {current_threshold*100:.2f})\n"
            f"**💡 推定勝率**: <b>{estimated_wr}</b>\n"
            f"**⭐ 期待値 (R:R)**: <code>1:{rr_ratio:.2f}</code>\n"
            f"**⬇️ ストップロス**: <code>{format_price_precision(stop_loss)}</code>\n"
            f"**⬆️ テイクプロフィット**: <code>{format_price_precision(take_profit)}</code>\n"
            f"**➡️ エントリー価格**: <code>{format_price_precision(entry_price)}</code>\n"
            f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        )
        
    elif context == "ポジション決済":
        # ポジション決済ではシグナル情報はスキップし、決済結果を優先
        pass
    
    # 取引実行結果のセクションを追加
    if trade_section:
        message += trade_section
        message += f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
    
    # 💡 失敗セクションがあれば追加
    if failure_section:
        message += failure_section + f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        
    # 💡 スコア詳細ブレークダウンは、シグナル通知のコンテキストでのみ、成功/失敗に関わらず追加する
    if context == "取引シグナル":
        message += (
            f" \n**📊 スコア詳細ブレークダウン** (+/-要因)\n"
            f"{breakdown_details}\n"
            f" <code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        )

    # ★ v19.0.47 修正点: BOT_VERSION を使用
    message += (f"<i>Bot Ver: {BOT_VERSION} - Full Analysis & Async Refactoring</i>")
    return message

def format_hourly_report(signals: List[Dict], attempt_log: Dict[str, str], start_time: float, current_threshold: float, bot_version: str) -> str:
    """ 1時間ごとの最高・最低スコア銘柄の通知メッセージを作成する。 """
    now_jst = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")
    start_jst = datetime.fromtimestamp(start_time, JST).strftime("%H:%M:%S")

    # スコアでソート
    signals_sorted = sorted(signals, key=lambda x: x['score'], reverse=True)
    
    analyzed_count = len(signals)
    attempt_count = len(attempt_log)
    
    # 分析試行されたが、クールダウンなどでスキップされた銘柄
    # 総監視銘柄数から、分析をスキップされた銘柄を計算
    total_monitoring_count = len(CURRENT_MONITOR_SYMBOLS)
    
    # 1時間で最もスコアが高かった銘柄 (Top 3)
    top_signals = signals_sorted[:TOP_SIGNAL_COUNT] 
    
    # 1時間で最もスコアが低かった銘柄 (Bottom 1)
    worst_signal = signals_sorted[-1] if signals_sorted else None
    
    header = (
        f"🔔 **Apex BOT - 1時間 定期レポート** 🔔\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - **期間**: {start_jst} - {datetime.now(JST).strftime('%H:%M:%S')} (JST)\n"
        f"  - **取引閾値 (Score)**: <code>{current_threshold*100:.2f} / 100</code>\n"
        f"  - **分析対象銘柄**: <code>{analyzed_count}</code> / {total_monitoring_count}\n"
        f"  - **取引スキップ銘柄**: <code>{attempt_count}</code>\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n\n"
    )
    
    message = header
    
    # Top Signals
    if top_signals:
        message += "🌟 **最高スコア銘柄 (Top Signals)**\n"
        for i, signal in enumerate(top_signals):
            message += (
                f"  - **{i+1}. {signal['symbol']}** ({signal['timeframe']})\n"
                f"    - **スコア**: <code>{signal['score'] * 100:.2f} / 100</code> (勝率: {get_estimated_win_rate(signal['score'])})\n"
                f"    - **R:R**: <code>1:{signal['rr_ratio']:.2f}</code>\n"
            )
        message += "\n"
    
    # Worst Signal (Bottom 1)
    if worst_signal:
         message += (
            f"\n"
            f"🔴 **ワーストスコア銘柄 (Bottom)**\n"
            f" - **銘柄**: <b>{worst_signal['symbol']}</b> ({worst_signal['timeframe']})\n"
            f" - **スコア**: <code>{worst_signal['score'] * 100:.2f} / 100</code>\n"
            f" - **推定勝率**: <code>{get_estimated_win_rate(worst_signal['score'])}</code>\n"
            f" - **指値 (Entry)**: <code>{format_price_precision(worst_signal['entry_price'])}</code>\n"
            f" - **SL/TP**: <code>{format_price_precision(worst_signal['stop_loss'])}</code> / <code>{format_price_precision(worst_signal['take_profit'])}</code>\n"
            f"\n"
        )

    # 試行されたがスキップされた銘柄のリスト（最初の5つまで）
    if attempt_log:
        message += "⚠️ **分析スキップ理由 (取引クールダウン/データ不足など)**\n"
        
        # 理由ごとに集計
        reason_counts = {}
        for symbol, reason in attempt_log.items():
            if reason not in reason_counts:
                reason_counts[reason] = []
            reason_counts[reason].append(symbol)

        for reason, symbols in reason_counts.items():
            symbol_list = ', '.join(symbols[:5])
            if len(symbols) > 5:
                symbol_list += f" ...他{len(symbols) - 5}件"
            
            message += f"  - **{reason}** ({len(symbols)}件): <code>{symbol_list}</code>\n"
        message += "\n"
        
    message += (
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"<i>Bot Ver: {bot_version} - Full Analysis & Async Refactoring</i>"
    )
    return message

def _to_json_compatible(data: Any) -> Any:
    """JSONシリアライズ可能でない型 (numpy, pandas) を標準のPython型に変換するヘルパー関数"""
    if isinstance(data, (np.ndarray, list)):
        return [_to_json_compatible(item) for item in data]
    elif isinstance(data, (pd.Series, pd.DataFrame)):
        return data.tolist()
    elif isinstance(data, (np.float64, float)):
        return float(data)
    elif isinstance(data, (np.int64, int)):
        return int(data)
    elif isinstance(data, (datetime)):
        return data.isoformat()
    return data

def log_signal(signal: Dict, context: str):
    """シグナルまたは取引結果をJSON形式でログに記録する"""
    log_data = {
        'timestamp_jst': datetime.now(JST).isoformat(),
        'context': context,
        'signal': _to_json_compatible(signal),
    }
    
    # シグナルのログはファイルに書き出すことも考慮し、json.dumpsで整形
    try:
        log_json = json.dumps(log_data, ensure_ascii=False, indent=4)
        logging.info(f"💾 SIGNAL LOG ({context}):\n{log_json}")
    except Exception as e:
        logging.error(f"❌ シグナルログのシリアライズに失敗: {e}")

# ====================================================================================
# EXCHANGE COMMUNICATION (CCXT Async)
# ====================================================================================

async def initialize_exchange_client():
    """CCXTクライアントを初期化する"""
    global EXCHANGE_CLIENT, IS_CLIENT_READY
    
    if not API_KEY or not SECRET_KEY:
        logging.error(f"❌ APIキーまたはシークレットキーが設定されていません。取引所: {CCXT_CLIENT_NAME.upper()}")
        IS_CLIENT_READY = False
        return
        
    try:
        # CCXTクライアントの動的ロードと初期化
        exchange_class = getattr(ccxt_async, CCXT_CLIENT_NAME.lower())
        
        # ★ 現物取引のため、'options': {'defaultType': 'spot'} を設定
        EXCHANGE_CLIENT = exchange_class({
            'apiKey': API_KEY,
            'secret': SECRET_KEY,
            'enableRateLimit': True, # レート制限を有効化
            'options': {
                'defaultType': 'spot', 
            },
        })
        
        # 接続テスト (取引所の情報取得)
        await EXCHANGE_CLIENT.load_markets()
        
        logging.info(f"✅ CCXTクライアント ({CCXT_CLIENT_NAME.upper()} / Spot) の初期化に成功しました。")
        IS_CLIENT_READY = True
        
    except Exception as e:
        logging.critical(f"❌ CCXTクライアントの初期化に失敗しました: {e}", exc_info=True)
        IS_CLIENT_READY = False

async def send_telegram_notification(message: str) -> bool:
    """Telegramにメッセージを送信する"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.warning("⚠️ TelegramのトークンまたはチャットIDが設定されていません。通知をスキップします。")
        return False
        
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        'chat_id': TELEGRAM_CHAT_ID,
        'text': message,
        'parse_mode': 'HTML' # MarkdownではなくHTML形式を使用
    }
    
    try:
        # requestsは同期ライブラリなので、asyncio.to_threadで実行
        response = await asyncio.to_thread(requests.post, url, data=payload, timeout=5)
        response.raise_for_status() # HTTPエラーが発生した場合に例外を投げる
        logging.info("✅ Telegram通知を送信しました。")
        return True
    except requests.exceptions.RequestException as e:
        logging.error(f"❌ Telegram通知の送信に失敗: {e}")
        return False

async def fetch_account_status() -> Dict:
    """口座のUSDT残高と総資産額を取得する"""
    global EXCHANGE_CLIENT, GLOBAL_TOTAL_EQUITY
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.error("❌ CCXTクライアントが未準備です。口座ステータスを取得できません。")
        return {'total_usdt_balance': 0.0, 'total_equity': 0.0, 'open_positions': [], 'error': True}
        
    try:
        # 1. 残高を取得
        balance = await EXCHANGE_CLIENT.fetch_balance()
        
        # 2. USDT (フリー) 残高を取得
        # 'free'は取引に使用可能な残高
        total_usdt_balance = balance.get('USDT', {}).get('free', 0.0)
        
        # 3. 総資産額 (Equity) を取得 (取引所によって取得方法が異なる)
        # ほとんどの取引所は 'total' のUSDT換算値か、または独自のEquityフィールドを持つ
        # ここではフォールバックとして、総USDT残高 (free + used) を使用する
        total_equity = balance.get('total', {}).get('USDT', total_usdt_balance)
        
        # CCXTが提供する Equity フィールドがあれば使用 (例: Bybit, Binanceなど)
        if 'info' in balance and 'totalAsset' in balance['info']:
            # 例えば、MEXCの現物口座では通常、USDTの合計残高が総資産として扱われる
            # CCXTの標準フォーマットに依存
            pass 
        
        # 総資産額として、USDTの合計額を最低限保証
        if total_equity < total_usdt_balance:
             total_equity = total_usdt_balance
             
        GLOBAL_TOTAL_EQUITY = total_equity # グローバル変数を更新
        
        logging.info(f"✅ 口座ステータス取得成功: Equity={format_usdt(GLOBAL_TOTAL_EQUITY)} USDT, Free USDT={format_usdt(total_usdt_balance)}")
        
        # 4. USDT以外の保有資産の評価
        open_positions = []
        for currency, amount in balance.get('total', {}).items():
            if currency not in ['USDT', 'USD'] and amount is not None and amount > 0.000001:
                try:
                    symbol = f"{currency}/USDT"
                    # シンボルが取引所に存在するか確認し、存在しない場合はハイフンなしの形式も試す
                    if symbol not in EXCHANGE_CLIENT.markets:
                        if f"{currency}USDT" in EXCHANGE_CLIENT.markets:
                            symbol = f"{currency}USDT"
                        else:
                            continue 
                            
                    ticker = await EXCHANGE_CLIENT.fetch_ticker(symbol)
                    usdt_value = amount * ticker['last']
                    
                    if usdt_value >= 10: # 10 USDT未満の保有は無視
                        open_positions.append({
                            'symbol': symbol,
                            'amount': amount,
                            'usdt_value': usdt_value
                        })
                except Exception as e:
                    logging.warning(f"⚠️ {currency} のUSDT価値を取得できませんでした（{EXCHANGE_CLIENT.name} GET {symbol}）。")

        return {
            'total_usdt_balance': total_usdt_balance,
            'total_equity': GLOBAL_TOTAL_EQUITY,
            'open_positions': open_positions,
            'error': False
        }
    except Exception as e:
        logging.error(f"❌ 口座ステータス取得中にエラーが発生: {e}", exc_info=True)
        return {'total_usdt_balance': 0.0, 'total_equity': 0.0, 'open_positions': [], 'error': True}

async def fetch_top_symbols() -> List[str]:
    """出来高TOPの銘柄リストを取得する"""
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY or SKIP_MARKET_UPDATE:
        logging.warning("⚠️ クライアント未準備またはマーケットアップデートがスキップされています。デフォルト銘柄リストを使用します。")
        return DEFAULT_SYMBOLS
        
    try:
        # 全ティッカーを取得
        tickers = await EXCHANGE_CLIENT.fetch_tickers()
        
        # USDTペアのみをフィルタリングし、出来高でソート
        usdt_pairs = []
        for symbol, ticker in tickers.items():
            # USDTで終わる現物ペアのみを対象とする
            if '/USDT' in symbol and ticker and ticker.get('quoteVolume', 0) is not None:
                 # 出来高 (quoteVolume) が一定以上あるものに絞る (ノイズ除去)
                if ticker['quoteVolume'] > 1000000: # 例: 1M USDT以上の出来高
                    usdt_pairs.append({'symbol': symbol, 'volume': ticker['quoteVolume']})

        # 出来高で降順にソート
        sorted_pairs = sorted(usdt_pairs, key=lambda x: x['volume'], reverse=True)
        
        # TOP_SYMBOL_LIMIT に加えて、デフォルトシンボルも確実に含める
        top_symbols = [p['symbol'] for p in sorted_pairs[:TOP_SYMBOL_LIMIT]]
        
        # デフォルトシンボルから、まだリストに含まれていないものを追加
        for default_symbol in DEFAULT_SYMBOLS:
            if default_symbol not in top_symbols:
                top_symbols.append(default_symbol)
        
        logging.info(f"✅ 出来高TOP銘柄リストを更新しました ({len(top_symbols)} 銘柄)。")
        return top_symbols

    except Exception as e:
        logging.error(f"❌ 出来高TOP銘柄の取得に失敗: {e}")
        return DEFAULT_SYMBOLS # 失敗した場合はデフォルトリストを使用

async def fetch_fgi_data() -> Dict:
    """Fear & Greed Index (FGI) データを取得する"""
    # 外部サービス (alternative.me) のAPIを使用
    # 注意: このAPIは時々不安定になる可能性があります
    url = "https://api.alternative.me/fng/?limit=1"
    
    try:
        # requestsは同期ライブラリなので、asyncio.to_threadで実行
        response = await asyncio.to_thread(requests.get, url, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        # データの解析
        if data and data.get('data'):
            fgi = data['data'][0]
            value = int(fgi['value'])
            value_classification = fgi['value_classification']
            
            # FGI値を -1.0 (Extreme Fear) から +1.0 (Extreme Greed) に正規化するプロキシ
            # FGIは0から100。50がニュートラル。
            fgi_proxy = (value - 50) / 50.0 
            
            logging.info(f"✅ FGIデータ取得成功: {value_classification} ({value}) / Proxy: {fgi_proxy:.2f}")
            
            return {
                'fgi_raw_value': f"{value} ({value_classification})",
                'fgi_proxy': fgi_proxy,
                'forex_bonus': 0.0, # 為替影響は今回は実装しないため 0.0
            }
        
    except Exception as e:
        logging.warning(f"⚠️ FGIデータ取得に失敗しました。デフォルト値を使用します: {e}")
        # 失敗した場合はニュートラルなデフォルト値を返す
        return GLOBAL_MACRO_CONTEXT.copy()

def calculate_dynamic_lot_size(score: float, account_status: Dict) -> float:
    """スコアと総資産額に基づいて動的な取引ロットサイズを計算する"""
    global GLOBAL_TOTAL_EQUITY, BASE_TRADE_SIZE_USDT
    
    total_equity = GLOBAL_TOTAL_EQUITY
    
    # 総資産額が不明な場合は、基本ロットサイズを使用
    if total_equity <= 0.0:
        return BASE_TRADE_SIZE_USDT
        
    # 最小ロット額（総資産の最小パーセント）と最大ロット額（総資産の最大パーセント）を計算
    min_lot = total_equity * DYNAMIC_LOT_MIN_PERCENT
    max_lot = total_equity * DYNAMIC_LOT_MAX_PERCENT
    
    # 最小ロット額はBASE_TRADE_SIZE_USDTより小さくならないようにする
    min_lot = max(min_lot, BASE_TRADE_SIZE_USDT)
    
    # スコアが最小閾値(SIGNAL_THRESHOLD_NORMAL)と最大スコア(DYNAMIC_LOT_SCORE_MAX)の間で線形補間
    min_score = SIGNAL_THRESHOLD_NORMAL
    max_score = DYNAMIC_LOT_SCORE_MAX
    
    if score <= min_score:
        # 閾値未満の場合は最小ロットを使用
        dynamic_lot = min_lot
    elif score >= max_score:
        # 最大スコア以上の場合は最大ロットを使用
        dynamic_lot = max_lot
    else:
        # 線形補間
        ratio = (score - min_score) / (max_score - min_score)
        dynamic_lot = min_lot + (max_lot - min_lot) * ratio
        
    # 利用可能なUSDT残高を超えないように制限
    # 'free'残高が使えない場合もあるため、ここでは安全を見てUSDT総額の90%を上限とする
    available_usdt = account_status.get('total_usdt_balance', 0.0)
    
    # 最小取引残高未満の場合は取引しない (メインループで処理されるが、念のためここでも制限)
    if available_usdt < MIN_USDT_BALANCE_FOR_TRADE:
         return 0.0 
         
    # 実際に取引に使えるロットサイズの上限を設定 (例: 利用可能USDT残高の90%)
    max_available_lot = available_usdt * 0.90 
    
    final_lot = min(dynamic_lot, max_available_lot)
    
    # 最小ロット額を下回らないようにする
    if final_lot < BASE_TRADE_SIZE_USDT:
         final_lot = BASE_TRADE_SIZE_USDT
         
    # 最小取引額（例: 10 USDTなど）よりも小さい場合は、取引不可と見なす
    if final_lot < 10.0:
        return 0.0
        
    logging.info(f"ℹ️ 動的ロット計算: Score={score*100:.2f} -> Lot={final_lot:.2f} USDT (Equity: {total_equity:.2f})")
    
    return final_lot

# ====================================================================================
# TECHNICAL ANALYSIS & SIGNAL GENERATION
# ====================================================================================

def calculate_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """OHLCVデータにテクニカル指標を追加する"""
    # データをコピーして操作
    df = df.copy() 
    
    # SMA (Simple Moving Average)
    df['SMA_20'] = ta.sma(df['close'], length=20)
    df['SMA_50'] = ta.sma(df['close'], length=50)
    df['SMA_200'] = ta.sma(df['close'], length=LONG_TERM_SMA_LENGTH) 
    
    # RSI (Relative Strength Index)
    df['RSI'] = ta.rsi(df['close'], length=14) 
    
    # MACD (Moving Average Convergence Divergence)
    macd_data = ta.macd(df['close'], fast=12, slow=26, signal=9)
    # MACD指標のキー名がccxt-specificでないことを確認して追加
    df['MACD'] = macd_data['MACD_12_26_9']
    df['MACDh'] = macd_data['MACDh_12_26_9']
    df['MACDs'] = macd_data['MACDs_12_26_9']
    
    # ATR (Average True Range) - SL/TP設定に必要
    df['ATR'] = ta.atr(df['high'], df['low'], df['close'], length=14)
    
    # Bollinger Bands
    bb_data = ta.bbands(df['close'], length=20, std=2.0)
    # 💡 【BBANDSキーの修正】 Key 'BBL_20_2.0' not found エラーに対応
    df['BBL'] = bb_data['BBL_20_2.0']
    df['BBM'] = bb_data['BBM_20_2.0']
    df['BBU'] = bb_data['BBU_20_2.0']
    df['BBB'] = bb_data['BBB_20_2.0']

    # OBV (On-Balance Volume)
    df['OBV'] = ta.obv(df['close'], df['volume'])
    df['OBV_SMA'] = ta.sma(df['OBV'], length=20)
    
    # Volume SMA (出来高の平均)
    df['Volume_SMA20'] = ta.sma(df['volume'], length=20)
    
    # NaN行を削除して、指標計算後に有効なデータのみを残す
    df = df.dropna().reset_index(drop=True)
    
    return df

def generate_signal_and_score(
    df: pd.DataFrame, 
    timeframe: str, 
    market_ticker: Dict, 
    macro_context: Dict, 
) -> Optional[Dict]:
    """
    指定されたデータフレームからロングシグナルを生成し、スコアリングする。
    ★ V19.0.44: データ取得・指標計算でデータ量が少なすぎた場合のみNoneを返す。
    """
    # 1. テクニカル指標の計算 (calculate_indicators で実行済みだが、欠損値処理のためにここでは最新データを確認)
    # calculate_indicators が NaN を drop しているため、有効なデータ数をチェック
    # 銘柄分析に最低限必要なデータ量 (例: ATR計算には14, SMA200計算には200のデータが必要なので、それより多く必要)
    # SMA200の計算には200本必要だが、計算後のデータが10本未満はリスク計算不可
    if len(df) < 10 or df['ATR'].isnull().all():
        logging.warning(f"⚠️ {market_ticker['symbol']} ({timeframe}): 指標計算後のデータが不足しています ({len(df)}本)。")
        # データ不足の場合は、スコアリング・シグナル生成をスキップ
        return None 
    
    last_candle = df.iloc[-1]
    last_close = last_candle['close']
    
    # ==================================================
    # 2. シグナルの基本判定 (ここではロングシグナルのみ)
    # ==================================================
    
    # A. 長期トレンドフィルター (SMA200)
    is_long_term_uptrend = last_close > last_candle['SMA_200']
    
    # B. RSIモメンタム (ロングの場合、RSIが50付近または上向き)
    rsi = last_candle['RSI']
    is_rsi_favorable = rsi >= RSI_MOMENTUM_LOW # 45以上
    
    # C. MACDヒストグラムがゼロラインに近づいている、またはプラスに転じている
    is_macd_favorable = last_candle['MACDh'] >= 0 or (last_candle['MACD'] > last_candle['MACDs'] and last_candle['MACDh'] < 0)
    
    # 総合的なシグナル発生条件 (必須条件)
    # 以下の条件を**全て**満たすことを必須とする
    if not (is_long_term_uptrend and is_rsi_favorable):
         # 必須条件を満たさない場合、シグナルは発生しない
         # ただし、スコアリングは実行し、ペナルティでスコアを下げる (スコアリングは続く)
         pass 

    # ==================================================
    # 3. リスク管理とSL/TPの計算 (ATRに基づく)
    # ==================================================
    
    atr = last_candle['ATR']
    if atr <= 0 or pd.isna(atr):
        # ATRが計算できない場合は取引不可
        logging.warning(f"⚠️ {market_ticker['symbol']} ({timeframe}): ATRが計算できません。取引スキップ。")
        return None
    
    # 【リスク許容度設定】
    # SL幅: 2.0 * ATR
    # TP幅: 4.0 * ATR (リスクリワード 1:2.0 を目指す)
    SL_MULTIPLIER = 2.0
    TP_MULTIPLIER = 4.0 
    
    sl_distance = atr * SL_MULTIPLIER
    tp_distance = atr * TP_MULTIPLIER
    
    # エントリー価格 (現物取引なので、指値価格は直近の終値とする)
    entry_price = last_close
    
    # ストップロス (SL) と テイクプロフィット (TP) 価格
    stop_loss = entry_price - sl_distance
    take_profit = entry_price + tp_distance
    
    # R:R比率の計算
    rr_ratio = tp_distance / sl_distance if sl_distance > 0 else 0.0
    
    # 価格が取引所の最小価格精度を満たしているか確認する必要があるが、CCXTで発注時に処理されるため、ここでは省略
    
    # 【価格制約チェック】
    # SLがゼロ以下になる場合、またはTPがSL以下になる場合は取引をスキップ
    if stop_loss <= 0.0 or take_profit <= stop_loss:
        logging.warning(f"⚠️ {market_ticker['symbol']} ({timeframe}): SL/TP計算に問題があります (SL:{stop_loss:.4f}, TP:{take_profit:.4f})。取引スキップ。")
        return None


    # ==================================================
    # 4. スコアリングの実行 (最大スコア 1.00)
    # ==================================================
    total_score = 0.0
    tech_data = {}

    # A. ベーススコア (ロングシグナルが発生した時点で付与)
    total_score += BASE_SCORE
    
    # B. 長期トレンド逆行ペナルティ (最大 0.30)
    long_term_reversal_penalty_value = 0.0
    if not is_long_term_uptrend:
        # 価格がSMA200をどの程度下回っているかに応じてペナルティを課す
        sma200 = last_candle['SMA_200']
        if last_close < sma200:
            price_diff_percent = abs(last_close - sma200) / sma200
            
            # 最大ペナルティ (0.30) を、乖離率が一定値 (例: 5%) を超えると適用
            # 5%の乖離で最大ペナルティとする (0.05 / 0.05 = 1.0)
            penalty_ratio = min(price_diff_percent / 0.05, 1.0) 
            long_term_reversal_penalty_value = LONG_TERM_REVERSAL_PENALTY * penalty_ratio
            total_score -= long_term_reversal_penalty_value

    tech_data['long_term_reversal_penalty_value'] = long_term_reversal_penalty_value

    # C. 中期トレンドアライメントボーナス (SMA50がSMA200の上にある場合: 最大 0.10)
    trend_alignment_bonus_value = 0.0
    if is_long_term_uptrend and last_candle['SMA_50'] > last_candle['SMA_200']:
        trend_alignment_bonus_value = TREND_ALIGNMENT_BONUS
        total_score += trend_alignment_bonus_value
        
    tech_data['trend_alignment_bonus_value'] = trend_alignment_bonus_value

    # D. 価格構造/ピボット支持ボーナス (直近の安値がSMA50/200や主要なサポートラインに近い場合: 最大 0.06)
    structural_pivot_bonus = 0.0
    # ここでは簡略化のため、価格がSMA50の近く(+-0.5%以内)にあることを条件とする
    sma50 = last_candle['SMA_50']
    price_diff_percent = abs(last_close - sma50) / sma50
    if is_long_term_uptrend and price_diff_percent < 0.005:
        structural_pivot_bonus = STRUCTURAL_PIVOT_BONUS
        total_score += structural_pivot_bonus
        
    tech_data['structural_pivot_bonus'] = structural_pivot_bonus

    # E. MACDクロス/発散ペナルティ (不利なクロスや発散: 最大 0.25)
    macd_penalty_value = 0.0
    if not is_macd_favorable:
        # MACDがシグナルを下回り、かつMACDhがマイナスの場合
        if last_candle['MACD'] <= last_candle['MACDs'] and last_candle['MACDh'] < 0:
            macd_penalty_value = MACD_CROSS_PENALTY
            total_score -= macd_penalty_value
            
    tech_data['macd_penalty_value'] = macd_penalty_value

    # F. RSIモメンタムボーナス (RSIが50より高く、買われすぎではない場合: 最大 0.10)
    rsi_momentum_bonus_value = 0.0
    tech_data['rsi_value'] = rsi
    # RSI 50で0点、70でRSI_MOMENTUM_BONUS_MAX (0.10)
    # RSI 50から70の間で線形にボーナスを増加させる
    if rsi > 50.0:
        # スコアリングは50から70の間で線形補間
        ratio = min(max((rsi - 50.0) / 20.0, 0.0), 1.0)
        rsi_momentum_bonus_value = RSI_MOMENTUM_BONUS_MAX * ratio
        total_score += rsi_momentum_bonus_value
        
    tech_data['rsi_momentum_bonus_value'] = rsi_momentum_bonus_value

    # G. OBV Momentum Bonus (OBVがSMAを上抜けている)
    obv_momentum_bonus_value = 0.0
    # 直近でOBVがOBV_SMAを上抜けしたこと
    if last_candle['OBV'] > last_candle['OBV_SMA'] and df['OBV'].iloc[-2] <= df['OBV_SMA'].iloc[-2]:
        obv_momentum_bonus_value = OBV_MOMENTUM_BONUS
        total_score += obv_momentum_bonus_value
        
    tech_data['obv_momentum_bonus_value'] = obv_momentum_bonus_value

    # H. Volume Spike Bonus
    volume_increase_bonus_value = 0.0
    if 'Volume_SMA20' in df.columns and last_candle['Volume_SMA20'] > 0 and last_candle['volume'] > last_candle['Volume_SMA20'] * 1.5: # 出来高が平均の1.5倍
        volume_increase_bonus_value = VOLUME_INCREASE_BONUS
        total_score += volume_increase_bonus_value
        
    tech_data['volume_increase_bonus_value'] = volume_increase_bonus_value

    # I. Volatility Penalty (ボリンジャーバンド幅が狭すぎる場合: 最大 0.05)
    volatility_penalty_value = 0.0
    bb_width_percent = last_candle['BBB']
    if bb_width_percent < VOLATILITY_BB_PENALTY_THRESHOLD * 100: # BB幅が1%未満
        # BB幅が狭いほどペナルティを重くする
        penalty_ratio = 1.0 - (bb_width_percent / (VOLATILITY_BB_PENALTY_THRESHOLD * 100))
        volatility_penalty_value = -0.05 * penalty_ratio # 最大 0.05 のマイナス
        total_score += volatility_penalty_value

    tech_data['volatility_penalty_value'] = volatility_penalty_value

    # J. 流動性ボーナス (板の厚みなど - ティッカーデータを使用)
    liquidity_bonus_value = 0.0
    # ここでは簡略化のため、24時間出来高 (quoteVolume) が1億USDT以上を最大ボーナスとする
    quote_volume = market_ticker.get('quoteVolume', 0) or 0
    if quote_volume > 0:
        # 1億USDTが出来高の最大基準
        max_volume = 100000000 
        # 出来高の規模に応じて最大ボーナス (0.07) を線形に配分
        ratio = min(quote_volume / max_volume, 1.0)
        liquidity_bonus_value = LIQUIDITY_BONUS_MAX * ratio
        total_score += liquidity_bonus_value

    tech_data['liquidity_bonus_value'] = liquidity_bonus_value
    
    # K. マクロ環境ボーナス/ペナルティ (FGI)
    fgi_proxy = macro_context.get('fgi_proxy', 0.0)
    sentiment_fgi_proxy_bonus = FGI_PROXY_BONUS_MAX * fgi_proxy # -0.05 から +0.05
    total_score += sentiment_fgi_proxy_bonus
    
    tech_data['sentiment_fgi_proxy_bonus'] = sentiment_fgi_proxy_bonus
    
    # スコアを最大 1.00 にクリップ
    final_score = min(total_score, 1.00)
    
    # ==================================================
    # 5. シグナルデータの構築
    # ==================================================
    signal = {
        'symbol': market_ticker['symbol'],
        'timeframe': timeframe,
        'timestamp': datetime.now(JST).isoformat(),
        'score': final_score,
        'entry_price': entry_price,
        'stop_loss': stop_loss,
        'take_profit': take_profit,
        'rr_ratio': rr_ratio,
        'tech_data': tech_data, # スコアの詳細データ
    }

    return signal


async def analyze_symbol(symbol: str) -> Optional[Dict]:
    """特定の銘柄について、全時間軸で分析し、最もスコアの高いシグナルを返す"""
    
    global EXCHANGE_CLIENT
    market_ticker = None
    
    try:
        # 1. ティッカー情報を取得 (流動性ボーナスに使用)
        market_ticker = await EXCHANGE_CLIENT.fetch_ticker(symbol)
        
        # 2. 全時間軸で並行してOHLCVを取得し、シグナルを生成
        analysis_tasks = [
            fetch_ohlcv_and_analyze(symbol, tf, market_ticker)
            for tf in TARGET_TIMEFRAMES
        ]
        
        # 全てのタスクを並行で実行
        results = await asyncio.gather(*analysis_tasks, return_exceptions=True)
        
        valid_signals = []
        for result in results:
            if isinstance(result, Exception):
                # エラーが発生した場合 (例: データ不足、取引所エラーなど)
                logging.warning(f"⚠️ {symbol}: 分析中にエラーが発生: {result}")
            elif result is not None:
                # 有効なシグナルが生成された場合
                valid_signals.append(result)

        if not valid_signals:
            return None
            
        # 3. 最もスコアの高いシグナルを選択
        best_signal = max(valid_signals, key=lambda s: s['score'])
        
        return best_signal

    except Exception as e:
        # fetch_tickerやその他の予期せぬエラー
        logging.error(f"❌ {symbol} の総合分析中にエラーが発生: {e}")
        return None

# ====================================================================================
# ORDER EXECUTION & MANAGEMENT
# ====================================================================================

async def adjust_order_amount(symbol: str, target_usdt_amount: float, price: float) -> Tuple[float, float]:
    """取引所のルールに基づいて注文数量を調整する"""
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        return 0.0, 0.0
        
    try:
        market = EXCHANGE_CLIENT.markets.get(symbol)
        if not market:
            logging.error(f"❌ 取引所 ({CCXT_CLIENT_NAME.upper()}) に {symbol} のマーケット情報がありません。")
            return 0.0, 0.0
            
        # 1. 目標USDT額からベース通貨の概算数量を計算
        base_amount_unrounded = target_usdt_amount / price
        
        # 2. 数量の精度を取得
        amount_precision = market['precision']['amount'] if market and market['precision'] else 4 # 4はデフォルト値
        # 最小数量 (小数点以下の桁数)
        min_amount = market['limits']['amount']['min'] if market and market['limits'] else 0.0001
        
        # 3. 数量の丸め (Truncation: 最小数量が0でない場合に適用)
        # 指数表記 (例: 1e-8) の精度を扱うために math.floor を使用
        factor = 10 ** amount_precision
        base_amount_rounded = math.floor(base_amount_unrounded * factor) / factor
        
        # 4. 最小数量チェック
        if base_amount_rounded < min_amount:
            logging.warning(f"⚠️ 調整後の数量 {base_amount_rounded:.8f} は最小数量 {min_amount:.8f} を下回りました。取引スキップ。")
            return 0.0, 0.0
            
        final_usdt_amount = base_amount_rounded * price
        
        return base_amount_rounded, final_usdt_amount
        
    except Exception as e:
        logging.error(f"❌ {symbol} の注文数量調整中にエラーが発生: {e}")
        return 0.0, 0.0

async def place_sl_tp_orders(
    symbol: str, 
    filled_amount: float, 
    stop_loss: float, 
    take_profit: float
) -> Dict:
    """
    現物ポジションのストップロス (SL) とテイクプロフィット (TP) 注文を同時に設定する。
    
    Returns: 
        {'status': 'ok', 'sl_order_id': '...', 'tp_order_id': '...'} 
        または 
        {'status': 'error', 'error_message': '...'}
    """
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY or filled_amount <= 0:
        return {'status': 'error', 'error_message': 'クライアント未準備または数量がゼロです。'}

    sl_order_id = None
    tp_order_id = None
    
    logging.info(f"⏳ SL/TP注文を設定中: {symbol} (Qty: {filled_amount:.4f}). SL={format_price_precision(stop_loss)}, TP={format_price_precision(take_profit)}")

    # 1. TP (テイクプロフィット) 指値売り注文の設定 (Limit Sell)
    try:
        # 数量の丸め (ここでは価格はTP価格を使用)
        # filled_amount が既に丸められているため、ここでは adjust_order_amount は使わず、filled_amount をそのまま使用
        amount_to_sell = filled_amount 
        
        # TP価格で指値売り
        tp_order = await EXCHANGE_CLIENT.create_order(
            symbol=symbol,
            type='limit', # 指値注文
            side='sell',  # 売り
            amount=amount_to_sell,
            price=take_profit,
            params={
                # OCO注文をサポートしていない取引所のため、単純な指値注文とする
                # CCXTは取引所独自のパラメータをサポート
                # 例: 'clientOrderId': f'TP-{uuid.uuid4()}'
            }
        )
        tp_order_id = tp_order.get('id', 'N/A')
        logging.info(f"✅ TP注文成功: {symbol}, ID: {tp_order_id}")
        
    except Exception as e:
        logging.error(f"❌ TP注文失敗 ({symbol}): {e}")
        # TP失敗は致命的ではないが、SL設定に影響が出る可能性があるため、SLは試行する
        # return {'status': 'error', 'error_message': f'TP注文失敗: {e}'}

    # 2. SL (ストップロス) ストップ指値売り注文の設定 (Stop Limit Sell)
    # 現物取引では、多くの取引所が「ストップ指値注文 (Stop Limit)」または「ストップ成行注文 (Stop Market)」
    # をサポートしている。ここでは汎用性の高い Stop Limit を試みる。
    try:
        amount_to_sell = filled_amount
        
        # ストップ価格: stop_loss (この価格に達するとトリガーされる)
        # リミット価格: stop_loss * 0.999 (スリッページを考慮し、少し低い価格で指値を設定)
        sl_limit_price = stop_loss * 0.999 
        
        # CCXTの標準的なストップロス注文の呼び出し
        sl_order = await EXCHANGE_CLIENT.create_order(
            symbol=symbol,
            type='stop_limit', # ストップ指値注文
            side='sell',       # 売り
            amount=amount_to_sell,
            price=sl_limit_price, # 指値価格
            params={
                'stopPrice': stop_loss, # トリガー価格
                # 例: 'clientOrderId': f'SL-{uuid.uuid4()}'
            }
        )
        sl_order_id = sl_order.get('id', 'N/A')
        logging.info(f"✅ SL注文成功: {symbol}, ID: {sl_order_id}")
        
    except ccxt.NotSupported as e:
        logging.warning(f"⚠️ {CCXT_CLIENT_NAME.upper()} は 'stop_limit' 注文をサポートしていない可能性があります。成行注文で再試行: {e}")
        # 取引所がストップ指値（stop_limit）をサポートしない場合、ストップ成行（stop_market）を試みる
        try:
             sl_order = await EXCHANGE_CLIENT.create_order(
                symbol=symbol,
                type='stop', # CCXTでのストップ成行注文の一般的なタイプ (取引所によって異なる)
                side='sell',
                amount=amount_to_sell,
                params={
                    'stopPrice': stop_loss, # トリガー価格
                    'triggerType': 'market', # 成行注文であることを明示 (取引所独自のパラメータ)
                }
            )
             sl_order_id = sl_order.get('id', 'N/A')
             logging.info(f"✅ SL注文成功 (Stop Market): {symbol}, ID: {sl_order_id}")
             
        except Exception as e_market:
            logging.critical(f"❌ SL注文失敗 (Stop Market/Limit): {symbol} - {e_market}")
            return {'status': 'error', 'error_message': f'SL注文失敗（TPもキャンセル）: {e_market}'}
            
    except Exception as e:
        logging.critical(f"❌ SL注文失敗 ({symbol}): {e}")
        return {'status': 'error', 'error_message': f'SL注文失敗（TPもキャンセル）: {e}'}
    
    
    # 3. どちらかの注文IDがN/Aの場合はエラーとする
    if sl_order_id == 'N/A' or tp_order_id == 'N/A':
        error_message = f"SL({sl_order_id}) または TP({tp_order_id}) の注文IDが取得できませんでした。"
        logging.critical(f"🚨 SL/TP設定失敗: {error_message}")
        return {'status': 'error', 'error_message': error_message}
        
    return {
        'status': 'ok',
        'sl_order_id': sl_order_id,
        'tp_order_id': tp_order_id
    }

async def close_position_immediately(symbol: str, amount: float) -> Dict:
    """ポジションを即座に成行売りでクローズする"""
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY or amount <= 0:
        return {'status': 'skipped', 'error_message': 'クライアント未準備または数量がゼロです。'}
        
    try:
        logging.warning(f"⚠️ 不完全ポジションを強制クローズ中: {symbol} (Qty: {amount:.4f})")
        
        # 成行売り注文を発注
        close_order = await EXCHANGE_CLIENT.create_order(
            symbol=symbol,
            type='market', # 成行注文
            side='sell',   # 売り
            amount=amount,
        )
        
        # 約定数量の確認
        closed_amount = close_order.get('filled', 0.0)
        
        if closed_amount > 0:
            logging.info(f"✅ 強制クローズ成功: {symbol} - {closed_amount:.4f} 数量を売却しました。")
            return {'status': 'ok', 'closed_amount': closed_amount}
        else:
            logging.error(f"❌ 強制クローズ失敗: 成行注文で約定が発生しませんでした。")
            return {'status': 'error', 'error_message': '成行売り注文が約定しませんでした。'}
            
    except Exception as e:
        logging.critical(f"❌ 強制クローズ中に致命的なエラーが発生 ({symbol}): {e}", exc_info=True)
        return {'status': 'error', 'error_message': f'強制クローズ失敗（APIエラー）: {e}'}


async def execute_trade(signal: Dict, account_status: Dict) -> Dict:
    """
    シグナルに基づいて現物指値買い注文を発注し、SL/TP注文を設定する。
    
    Args:
        signal: シグナルデータ (symbol, entry_price, stop_loss, take_profitなどを含む)
        account_status: 口座残高情報
        
    Returns:
        取引結果辞書
    """
    global EXCHANGE_CLIENT
    symbol = signal['symbol']
    entry_price = signal['entry_price']
    
    # 1. 動的ロットサイズの計算
    lot_size_usdt = calculate_dynamic_lot_size(signal['score'], account_status)
    signal['lot_size_usdt'] = lot_size_usdt # シグナルデータにロットサイズを保存
    
    # 2. 注文数量の調整
    # 注文価格: entry_price (シグナルで決定した指値価格)
    base_amount_to_buy, final_usdt_amount = await adjust_order_amount(symbol, lot_size_usdt, entry_price)
    
    if base_amount_to_buy <= 0.0:
        return {'status': 'error', 'error_message': '調整後の数量が取引所の最小要件を満たしません。'}
        
    logging.info(f"⏳ 現物指値買い注文を発注中: {symbol} @ {format_price_precision(entry_price)} (Qty: {base_amount_to_buy:.4f})")
    
    try:
        # 3. IOC指値買い注文の発注
        # type='limit' と timeInForce: 'IOC' の組み合わせにより、指値注文（約定価格の確実性）かつ即時約定失効（未約定分を残さない）を実現
        order = await EXCHANGE_CLIENT.create_order(
            symbol=symbol,
            type='limit', # 指値注文 (IOCを適用)
            side='buy',  # 買い
            amount=base_amount_to_buy,
            price=entry_price,
            params={
                'timeInForce': 'IOC', # Immediate Or Cancel (即時約定失効)
            }
        )
        
        order_id = order.get('id', 'N/A')
        filled_amount = order.get('filled', 0.0)
        filled_price = order.get('average', entry_price) # 約定平均価格 (IOCなのでほぼエントリー価格)
        
        if filled_amount > 0.0:
            filled_usdt = filled_amount * filled_price
            
            logging.info(f"✅ IOC指値買い注文約定成功: {symbol} - {filled_amount:.4f} 数量 (平均価格: {format_price_precision(filled_price)})")

            # 4. SL/TP注文の発注
            sl_tp_result = await place_sl_tp_orders(
                symbol=symbol,
                filled_amount=filled_amount,
                stop_loss=signal['stop_loss'],
                take_profit=signal['take_profit']
            )
            
            if sl_tp_result['status'] == 'ok':
                # ポジション管理リストに追加
                new_position = {
                    'id': str(uuid.uuid4()), # ユニークなポジションID
                    'symbol': symbol,
                    'entry_price': filled_price,
                    'filled_amount': filled_amount,
                    'filled_usdt': filled_usdt,
                    'stop_loss': signal['stop_loss'],
                    'take_profit': signal['take_profit'],
                    'sl_order_id': sl_tp_result['sl_order_id'],
                    'tp_order_id': sl_tp_result['tp_order_id'],
                    'open_time': time.time(),
                }
                OPEN_POSITIONS.append(new_position)
                
                return {
                    'status': 'ok',
                    'filled_amount': filled_amount,
                    'filled_usdt': filled_usdt,
                    'entry_price': filled_price,
                    'sl_order_id': sl_tp_result['sl_order_id'],
                    'tp_order_id': sl_tp_result['tp_order_id'],
                }
            else:
                # 5. SL/TP設定に失敗した場合 -> ポジションを即座にクローズ
                logging.critical(f"🚨 SL/TP設定に失敗したため、ポジションを強制クローズします: {sl_tp_result['error_message']}")
                
                # 成行売りでクローズ
                close_result = await close_position_immediately(symbol, filled_amount)
                
                return {
                    'status': 'error',
                    'error_message': f'IOC約定後にSL/TP設定に失敗: {sl_tp_result["error_message"]}',
                    'close_status': close_result['status'],
                    'closed_amount': close_result.get('closed_amount', 0.0),
                    'close_error_message': close_result.get('error_message'),
                }
        else:
            # 約定しなかった場合 (filled_amount == 0.0)
            error_message = '指値買い注文が即時約定しなかったためキャンセルされました。'
            
            # 💡 V19.0.52 修正: 失敗時の最終注文ステータスをログに記録
            final_status = order.get('status', 'N/A')
            logging.error(f"❌ 最終的なIOC注文ステータス: ID={order_id}, Status={final_status}, Filled={filled_amount:.4f}")
            
            # 💡 ユーザーの報告エラーメッセージに合わせて、もしユーザーがMarket注文に変えていた場合を考慮
            if order.get('type') == 'market':
                error_message = '成行買い注文で約定が発生しませんでした。（即時約定量がゼロ）'
                
            logging.info(f"ℹ️ 取引スキップ: {error_message}")
            return {'status': 'error', 'error_message': error_message, 'close_status': 'skipped'}
            
    except ccxt.NetworkError as e:
        error_message = f"ネットワークエラー: {e}"
        logging.error(f"❌ 取引実行失敗 ({symbol}): {error_message}", exc_info=True)
        return {'status': 'error', 'error_message': error_message, 'close_status': 'skipped'}
        
    except ccxt.ExchangeError as e:
        error_message = f"取引所エラー: {e}"
        logging.error(f"❌ 取引実行失敗 ({symbol}): {error_message}", exc_info=True)
        
        # 💡 CCXTエラーでも約定している可能性を考慮:
        # このエラーが返された時点で購入が成功し、SL/TP設定中にエラーが発生した可能性
        filled_amount_unknown = base_amount_to_buy # 注文した数量を暫定として強制クローズを試みる
        close_result = await close_position_immediately(symbol, filled_amount_unknown)
        
        return {
            'status': 'error',
            'error_message': f'取引所エラー（IOC/SL/TP設定失敗）: {e}',
            'close_status': close_result['status'],
            'closed_amount': close_result.get('closed_amount', 0.0),
            'close_error_message': close_result.get('error_message'),
        }
        
    except Exception as e:
        error_message = f"不明なエラー: {e}"
        logging.critical(f"❌ 取引実行中に予期せぬエラーが発生 ({symbol}): {e}", exc_info=True)
        return {'status': 'error', 'error_message': error_message, 'close_status': 'skipped'}

async def cancel_single_order(order_id: str, symbol: str) -> bool:
    """単一の注文をキャンセルするヘルパー関数"""
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY or order_id == 'N/A':
        return False
        
    try:
        await EXCHANGE_CLIENT.cancel_order(order_id, symbol)
        logging.info(f"✅ 注文キャンセル成功: {symbol} ID: {order_id}")
        return True
    except Exception as e:
        # 既にキャンセルされている、または約定済みの場合はエラーにならないことが多い
        logging.warning(f"⚠️ 注文キャンセル失敗 ({symbol} ID: {order_id}): {e}")
        return False

async def open_order_management_loop():
    """オープン中のポジションのSL/TP注文の状態を監視する (10秒ごと)"""
    global EXCHANGE_CLIENT, OPEN_POSITIONS
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.warning("⚠️ 注文監視ループ: クライアントが未準備です。スキップします。")
        return
        
    if not OPEN_POSITIONS:
        logging.debug("ℹ️ 注文監視ループ: 管理中のオープンポジションはありません。")
        return

    positions_to_remove_ids = []
    
    # 全ポジションをイテレーション
    for position in OPEN_POSITIONS:
        symbol = position['symbol']
        is_closed = False
        
        try:
            # 1. SLとTPの注文状況を並行して取得
            # ここでは、fetch_orderではなく、fetch_open_ordersで注文IDをチェックする方が効率的
            # ただし、fetch_orderの方が注文の状態変化を正確に取得できる
            
            sl_open = False
            tp_open = False
            
            # SL注文の状態確認
            try:
                sl_order = await EXCHANGE_CLIENT.fetch_order(position['sl_order_id'], symbol)
                # 注文が 'open' または 'partial' であればオープン中と見なす
                if sl_order.get('status') in ['open', 'partial']:
                    sl_open = True
                elif sl_order.get('status') in ['closed', 'canceled']:
                    # 既に決済されている、またはキャンセルされている
                    sl_open = False 
                    
            except Exception as e:
                # 注文が見つからない/APIエラーなど -> 決済済みまたはキャンセル済みと見なす
                sl_open = False
            
            # TP注文の状態確認
            try:
                tp_order = await EXCHANGE_CLIENT.fetch_order(position['tp_order_id'], symbol)
                if tp_order.get('status') in ['open', 'partial']:
                    tp_open = True
                elif tp_order.get('status') in ['closed', 'canceled']:
                    tp_open = False
                    
            except Exception as e:
                tp_open = False
            
            
            # 💡 V19.0.53: SL/TP注文の状態による処理の分岐
            if not sl_open and not tp_open:
                # 1. 両方の決済注文が消滅 -> SLまたはTPで決済完了と見なす
                is_closed = True
                exit_type = "取引所決済完了"
                logging.info(f"🔴 決済検出: {position['symbol']} - SL/TP注文が取引所から消滅。決済完了と見なします。")
                
            elif not sl_open or not tp_open:
                 # 2. 片方の決済注文が消滅または未設定 (再設定が必要なケース)
                logging.warning(f"⚠️ {position['symbol']} の決済注文が不完全です (SL Open:{sl_open}, TP Open:{tp_open})。再設定を試みます。")
                
                # A. 残っている注文をキャンセルする (二重注文を防ぐため)
                if sl_open:
                    await cancel_single_order(position['sl_order_id'], position['symbol'])
                if tp_open:
                    await cancel_single_order(position['tp_order_id'], position['symbol'])

                # B. SL/TPを再設定
                re_place_result = await place_sl_tp_orders(
                    symbol=position['symbol'],
                    filled_amount=position['filled_amount'],
                    stop_loss=position['stop_loss'],
                    take_profit=position['take_profit']
                )
                
                if re_place_result['status'] == 'ok':
                    # 新しい注文IDでポジション情報を更新
                    position['sl_order_id'] = re_place_result['sl_order_id']
                    position['tp_order_id'] = re_place_result['tp_order_id']
                    logging.info(f"✅ {position['symbol']} のSL/TP注文を再設定しました。新しいIDを登録しました。")
                else:
                    logging.critical(f"🚨 {position['symbol']} のSL/TP再設定に失敗しました: {re_place_result['error_message']}。手動で確認してください。")
            
            else:
                # 3. 両方の決済注文が残っている -> ポジションオープン中
                logging.debug(f"ℹ️ {position['symbol']} は引き続きオープン中 (SL: {sl_open}, TP: {tp_open})")
                pass
            
            
            if is_closed:
                positions_to_remove_ids.append(position['id'])
                
                # 約定価格は履歴から取得が必要だが、ここでは簡略化のため0.0とする
                closed_result = {
                    'symbol': position['symbol'],
                    'entry_price': position['entry_price'],
                    'stop_loss': position['stop_loss'],
                    'take_profit': position['take_profit'],
                    'filled_usdt': position['filled_usdt'],
                    'close_price': 0.0, # 未取得
                    'pnl': 0.0, # 未計算
                    'pnl_percent': 0.0, # 未計算
                    'exit_type': exit_type,
                }
                
                # 💡 Telegram通知を発射 (約定価格と損益は未確定)
                notification_message = format_telegram_message(
                    signal=position, 
                    context="ポジション決済", 
                    current_threshold=get_current_threshold(GLOBAL_MACRO_CONTEXT),
                    trade_result=closed_result,
                    exit_type=exit_type
                )
                await send_telegram_notification(notification_message)
                log_signal(closed_result, f"CLOSED_{exit_type}")
                
        except Exception as e:
            logging.error(f"❌ {position['symbol']} の注文監視中にエラーが発生: {e}")
            continue

    # 決済が確認されたポジションをリストから削除
    if positions_to_remove_ids:
        OPEN_POSITIONS = [p for p in OPEN_POSITIONS if p['id'] not in positions_to_remove_ids]
        logging.info(f"🗑️ {len(positions_to_remove_ids)} 件の決済済みポジションを管理リストから削除しました。")


async def fetch_ohlcv_and_analyze(symbol: str, tf: str, market_ticker: Dict) -> Optional[Dict]:
    """OHLCVを取得し、指標計算とスコアリングを行うヘルパー関数。"""
    try:
        global EXCHANGE_CLIENT
        # OHLCVを取得
        ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(symbol, tf, limit=REQUIRED_OHLCV_LIMITS[tf])
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')

        # テクニカル指標を計算
        df = calculate_indicators(df.copy())
        
        # シグナルとスコアを生成
        signal = generate_signal_and_score(
            df=df,
            timeframe=tf,
            market_ticker=market_ticker,
            macro_context=GLOBAL_MACRO_CONTEXT
        )
        return signal
        
    except Exception as e:
        # このエラーは analyze_symbol でキャッチされる
        raise Exception(f"OHLCV Fetch/Indicator Calc Error for {symbol} ({tf}): {e}")

# ====================================================================================
# MAIN BOT LOGIC
# ====================================================================================

async def main_bot_loop():
    """ボットのメイン実行ループ (1分ごと)"""
    global LAST_SUCCESS_TIME, LAST_SIGNAL_TIME, LAST_ANALYSIS_SIGNALS, CURRENT_MONITOR_SYMBOLS, GLOBAL_MACRO_CONTEXT, LAST_HOURLY_NOTIFICATION_TIME, IS_FIRST_MAIN_LOOP_COMPLETED, HOURLY_SIGNAL_LOG, HOURLY_ATTEMPT_LOG, BOT_VERSION
    
    start_time = time.time()
    now_jst = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")
    logging.info(f"--- 💡 {now_jst} - BOT LOOP START (M1 Frequency) ---")
    
    # 1. FGIデータを取得し、グローバルマクロコンテキストを更新
    GLOBAL_MACRO_CONTEXT = await fetch_fgi_data()
    # FGIの値をスコアリングに反映する準備
    macro_influence_score = (GLOBAL_MACRO_CONTEXT.get('fgi_proxy', 0.0) * FGI_PROXY_BONUS_MAX)
    
    # 2. 取引閾値を決定
    current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
    
    # 3. 監視銘柄リストを更新 (最初のループでのみ実行)
    if not IS_FIRST_MAIN_LOOP_COMPLETED and not SKIP_MARKET_UPDATE:
        CURRENT_MONITOR_SYMBOLS = await fetch_top_symbols()
        
    # 4. 全監視銘柄について分析を並行実行
    analysis_tasks = []
    
    # 1時間ごとのレポートのための準備
    if time.time() - LAST_HOURLY_NOTIFICATION_TIME >= HOURLY_SCORE_REPORT_INTERVAL:
        HOURLY_SIGNAL_LOG = [] # リセット
        HOURLY_ATTEMPT_LOG = {} # リセット

    
    for symbol in CURRENT_MONITOR_SYMBOLS:
        # クールダウンチェック (2時間以内はスキップ)
        last_signal_time = LAST_SIGNAL_TIME.get(symbol, 0.0)
        if time.time() - last_signal_time < TRADE_SIGNAL_COOLDOWN:
            HOURLY_ATTEMPT_LOG[symbol] = "取引クールダウン中"
            continue
            
        analysis_tasks.append(analyze_symbol(symbol))
        
    logging.info(f"🔍 {len(analysis_tasks)} 銘柄の分析を並行して開始します...")
    
    # 全ての分析タスクを実行し、結果を取得
    all_signals = await asyncio.gather(*analysis_tasks)
    
    valid_signals = [s for s in all_signals if s is not None]
    LAST_ANALYSIS_SIGNALS = valid_signals # 最後の分析結果を保存
    
    # 5. ベストシグナルを特定し、取引閾値を確認
    best_signal = None
    if valid_signals:
        # スコアで降順にソート
        valid_signals_sorted = sorted(valid_signals, key=lambda s: s['score'], reverse=True)
        best_signal = valid_signals_sorted[0]
        
        # 1時間ごとのレポート用にすべての有効シグナルを保存
        HOURLY_SIGNAL_LOG.extend(valid_signals)
        
        # 最高スコアが閾値を超えているかチェック
        if best_signal['score'] >= current_threshold:
            logging.info(f"🔥 最高シグナル: {best_signal['symbol']} ({best_signal['timeframe']}) - Score: {best_signal['score'] * 100:.2f}")
        else:
             logging.info(f"ℹ️ 最高シグナル: {best_signal['symbol']} ({best_signal['timeframe']}) - Score: {best_signal['score'] * 100:.2f} (閾値未満)")
    else:
        logging.info("ℹ️ 今回のループでは有効なシグナルは見つかりませんでした。")
    
    # 6. 取引の実行
    trade_result = None
    account_status = await fetch_account_status() # 最新の残高を取得
    
    if best_signal and not TEST_MODE:
        # A. 閾値チェック
        if best_signal['score'] >= current_threshold:
            # B. 残高チェック
            if account_status.get('total_usdt_balance', 0.0) >= MIN_USDT_BALANCE_FOR_TRADE:
                # C. 取引実行
                trade_result = await execute_trade(best_signal, account_status)
                
                if trade_result['status'] == 'ok':
                    LAST_SIGNAL_TIME[best_signal['symbol']] = time.time() # クールダウンタイマーをリセット
                    LAST_SUCCESS_TIME = time.time() # 成功時刻を更新
                else:
                    # 取引失敗 (APIエラー、約定せずなど)
                    logging.error(f"❌ {best_signal['symbol']} の取引実行に失敗: {trade_result['error_message']}")
            else:
                error_message = f"残高不足 (現在: {format_usdt(account_status['total_usdt_balance'])} USDT)。新規取引に必要な額: {MIN_USDT_BALANCE_FOR_TRADE:.2f} USDT。"
                trade_result = {'status': 'error', 'error_message': error_message, 'close_status': 'skipped'}
                logging.warning(f"⚠️ {best_signal['symbol']} 取引スキップ: {error_message}")
        else:
            logging.info(f"ℹ️ {best_signal['symbol']} は閾値 {current_threshold*100:.2f} を満たしていません。取引をスキップします。")
            
    # 7. Telegram通知
    if trade_result and trade_result.get('status') == 'ok':
        # 取引成功
        # ★ v19.0.47 修正点: BOT_VERSION を明示的に渡す
        notification_message = format_telegram_message(best_signal, "取引シグナル", current_threshold, trade_result)
        await send_telegram_notification(notification_message)
        log_signal(best_signal, "TRADE_SUCCESS")
        
    elif trade_result and trade_result.get('status') == 'error':
        # 取引失敗 (失敗の詳細をユーザーに通知)
        notification_message = format_telegram_message(best_signal, "取引シグナル", current_threshold, trade_result)
        await send_telegram_notification(notification_message)
        log_signal(best_signal, "TRADE_FAILURE")
        
    # 8. 初回起動完了通知
    if not IS_FIRST_MAIN_LOOP_COMPLETED:
        IS_FIRST_MAIN_LOOP_COMPLETED = True
        startup_message = format_startup_message(account_status, GLOBAL_MACRO_CONTEXT, len(CURRENT_MONITOR_SYMBOLS), current_threshold, BOT_VERSION)
        await send_telegram_notification(startup_message)
        logging.info("✅ 初回メインループが完了しました。")

    # 9. 1時間ごとのレポート通知
    if IS_FIRST_MAIN_LOOP_COMPLETED and (time.time() - LAST_HOURLY_NOTIFICATION_TIME >= HOURLY_SCORE_REPORT_INTERVAL):
        if HOURLY_SIGNAL_LOG:
            report_message = format_hourly_report(HOURLY_SIGNAL_LOG, HOURLY_ATTEMPT_LOG, LAST_HOURLY_NOTIFICATION_TIME, current_threshold, BOT_VERSION)
            await send_telegram_notification(report_message)
            LAST_HOURLY_NOTIFICATION_TIME = time.time()
            HOURLY_SIGNAL_LOG = [] # 通知後にリセット
            HOURLY_ATTEMPT_LOG = {} # 通知後にリセット
        else:
             logging.info("ℹ️ 1時間レポート: 有効なシグナルがなかったため、通知をスキップします。")
             LAST_HOURLY_NOTIFICATION_TIME = time.time() # 通知はスキップするが時刻は更新

    logging.info(f"--- 💡 BOT LOOP END (Elapsed: {time.time() - start_time:.2f}s) ---")


async def bot_scheduler():
    """メインループを定期実行するスケジューラ"""
    
    # CCXTクライアントの初期化を待つ
    # initialize_exchange_client() は startup_event で実行される
    await asyncio.sleep(5) 
    
    # 致命的エラー時の連続実行を防ぐためのカウンター
    consecutive_failure_count = 0
    MAX_CONSECUTIVE_FAILURES = 5
    
    # 💡 初回の通知時刻を現在時刻に設定
    global LAST_HOURLY_NOTIFICATION_TIME
    LAST_HOURLY_NOTIFICATION_TIME = time.time()

    while True:
        try:
            if not IS_CLIENT_READY:
                 logging.error("❌ クライアントが未準備のため、メインループをスキップします。")
                 await initialize_exchange_client() # 再初期化を試みる
                 await asyncio.sleep(MONITOR_INTERVAL)
                 continue
                 
            await main_bot_loop()
            consecutive_failure_count = 0 # 成功したらリセット

        except Exception as e:
            consecutive_failure_count += 1
            logging.critical(f"❌ メインボットループで致命的なエラーが発生 (連続失敗: {consecutive_failure_count}/{MAX_CONSECUTIVE_FAILURES}): {e}", exc_info=True)

            if consecutive_failure_count >= MAX_CONSECUTIVE_FAILURES:
                # 連続失敗が閾値を超えたら、停止し、Telegramに通知
                logging.critical(f"🚨 連続失敗回数が閾値 ({MAX_CONSECUTIVE_FAILURES}) を超えました。BOTを停止します。")
                
                # 致命的エラー通知
                try:
                    await send_telegram_notification(f"🚨 **致命的エラーによりBOTが停止しました**\n連続して {MAX_CONSECUTIVE_FAILURES} 回の失敗が発生しました。ログを確認してください。\n<i>Bot Ver: {BOT_VERSION}</i>")
                except Exception as notify_e:
                     logging.error(f"❌ 致命的エラー通知の送信に失敗: {notify_e}")
                
                # ループを抜けて停止
                break 

        # 次のループまで待機
        await asyncio.sleep(LOOP_INTERVAL)


async def open_order_management_scheduler():
    """オープン注文監視ループを定期実行するスケジューラ (10秒ごと)"""
    # 初回起動後の待機時間を考慮し、初回は少し遅延させて実行
    await asyncio.sleep(15)
    
    while True:
        try:
            # メインループに影響を与えないように、監視は別タスクで実行
            await open_order_management_loop() 
        except Exception as e:
            # 注文監視のエラーは致命的ではないことが多いが、ログに記録
            logging.error(f"❌ 注文監視ループ実行中にエラーが発生: {e}")
            
        # 次のループまで待機
        await asyncio.sleep(MONITOR_INTERVAL)


# ====================================================================================
# FASTAPI & ENTRY POINT
# ====================================================================================

# FastAPIアプリケーションの初期化
# ★ v19.0.53 修正点: BOT_VERSION を使用
app = FastAPI(title="Apex BOT API", version=BOT_VERSION)

@app.on_event("startup")
async def startup_event():
    """アプリケーション起動時にCCXTクライアントを初期化し、メインのタスクを開始する"""
    logging.info("🚀 BOTの起動処理を開始します...")
    
    # CCXTクライアントの初期化
    await initialize_exchange_client()
    
    # メインBOTロジックをバックグラウンドタスクとして実行
    asyncio.create_task(bot_scheduler())
    
    # 注文監視ロジックをバックグラウンドタスクとして実行
    asyncio.create_task(open_order_management_scheduler())

@app.on_event("shutdown")
async def shutdown_event():
    """アプリケーション終了時にCCXTクライアントをクローズする"""
    global EXCHANGE_CLIENT
    if EXCHANGE_CLIENT:
        await EXCHANGE_CLIENT.close()
        logging.info("🛑 CCXTクライアントを正常にクローズしました。")

@app.get("/")
async def health_check():
    """ヘルスチェックエンドポイント"""
    status = {
        "status": "ok" if IS_CLIENT_READY else "error",
        "message": f"Apex BOT {BOT_VERSION} is running.",
        "exchange_client": CCXT_CLIENT_NAME.upper(),
        "test_mode": TEST_MODE,
        "last_success_time": datetime.fromtimestamp(LAST_SUCCESS_TIME, JST).strftime("%Y/%m/%d %H:%M:%S") if LAST_SUCCESS_TIME > 0 else "N/A"
    }
    return JSONResponse(content=status)

@app.get("/status")
async def get_bot_status():
    """BOTの現在の状態とポジション情報を返す"""
    
    # 最新の残高とマクロコンテキストを取得
    account_status = await fetch_account_status()
    macro_context = GLOBAL_MACRO_CONTEXT
    current_threshold = get_current_threshold(macro_context)
    
    # 管理中のポジションのサマリー
    positions_summary = []
    for pos in OPEN_POSITIONS:
        positions_summary.append({
            "symbol": pos['symbol'],
            "entry_price": format_price_precision(pos['entry_price']),
            "filled_usdt": format_usdt(pos['filled_usdt']),
            "stop_loss": format_price_precision(pos['stop_loss']),
            "take_profit": format_price_precision(pos['take_profit']),
            "open_time_jst": datetime.fromtimestamp(pos['open_time'], JST).strftime("%Y/%m/%d %H:%M:%S"),
            "sl_order_id": pos['sl_order_id'],
            "tp_order_id": pos['tp_order_id'],
        })

    response_data = {
        "bot_version": BOT_VERSION,
        "exchange": CCXT_CLIENT_NAME.upper(),
        "is_client_ready": IS_CLIENT_READY,
        "test_mode": TEST_MODE,
        "monitoring_symbols_count": len(CURRENT_MONITOR_SYMBOLS),
        "trade_threshold_score": f"{current_threshold*100:.2f}",
        "macro_context": {
            "fgi_raw_value": macro_context['fgi_raw_value'],
            "fgi_proxy": f"{macro_context['fgi_proxy']:.2f}",
        },
        "account_status": {
            "total_equity_usdt": format_usdt(account_status['total_equity']),
            "usdt_free_balance": format_usdt(account_status['total_usdt_balance']),
            "error": account_status['error']
        },
        "open_positions": positions_summary,
        "last_signals_count": len(LAST_ANALYSIS_SIGNALS),
    }
    
    return JSONResponse(content=response_data)


# エントリポイント
if __name__ == "__main__":
    # uvicornサーバーの起動
    # Port 8000 で起動
    uvicorn.run(app, host="0.0.0.0", port=8000)
