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
CCXT_CLIENT_NAME = os.getenv("EXCHANGE_CLIENT", "mexc") # ★デフォルトはmexc
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
            if '指値買い注文が即時約定しなかったためキャンセルされました' in error_message:
                 trade_status_line = f"❌ **自動売買 失敗**: 指値買い注文が即時約定しなかったためキャンセルされました。"
            elif '成行買い注文で約定が発生しませんでした' in error_message:
                 trade_status_line = f"❌ **自動売買 失敗**: 成行買い注文で約定が発生しませんでした。"
            else:
                 # SL/TP設定失敗など、より深刻なエラーを含む
                 trade_status_line = f"❌ **自動売買 失敗**: {error_message}"
            
            # 💡 【欠損箇所補完】取引失敗詳細セクションの生成
            # SL/TP設定失敗後の強制クローズの結果を詳細に表示する
            if trade_result and trade_result.get('status') == 'error' and trade_result.get('close_status') != 'skipped':
                # SL/TP失敗後の強制クローズ試行があった場合
                failure_section_lines = []
                
                # 強制クローズ結果の表示
                if trade_result.get('close_status') == 'ok':
                     failure_section_lines.append(f"  - **強制クローズ結果**: ✅ 成功 ({trade_result['closed_amount']:.4f} 数量売却)")
                elif trade_result.get('close_status') == 'error':
                     failure_section_lines.append(f"  - **強制クローズ結果**: ❌ 失敗 (エラー: {trade_result.get('close_error_message', '不明')})")
                else:
                     failure_section_lines.append(f"  - **強制クローズ結果**: ➖ 未実行/不明")

                # 注文ID (IOC注文)
                order_id_display = trade_result.get('order_id', 'N/A')
                failure_section_lines.append(f"  - **初期注文ID**: <code>{order_id_display}</code>")
                
                # 元のエラーメッセージ
                failure_section_lines.append(f"  - **元エラー**: {trade_result['error_message']}")

                failure_section = (
                    f"\n<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
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
        # positionがsignalとして渡される
        entry_price = signal.get('entry_price', 0.0)
        exit_price = signal.get('exit_price', 0.0)
        filled_amount = signal.get('filled_amount', 0.0)
        filled_usdt = signal.get('filled_usdt', 0.0) # 投入USDT額
        pnl = signal.get('pnl', 0.0)
        pnl_percent = signal.get('pnl_percent', 0.0)
        
        is_pnl_positive = pnl >= 0
        pnl_sign = '🟢' if is_pnl_positive else '🔴'
        
        if exit_type == 'TP':
            trade_status_line = f"{pnl_sign} **ポジション決済**: テイクプロフィット (TP) 達成！"
        elif exit_type == 'SL':
            trade_status_line = f"{pnl_sign} **ポジション決済**: ストップロス (SL) 発動。"
        elif exit_type == '取引所決済完了':
            trade_status_line = f"⚠️ **ポジション決済**: 取引所側で決済注文が完了しました。"
        else: # Forced Close, Manual Close, Unknown
             trade_status_line = f"{pnl_sign} **ポジション決済**: 強制クローズまたは手動決済。"


        trade_section = (
            f"💰 **決済結果**\n"
            f"  - **ポジション額**: <code>{format_usdt(filled_usdt)}</code> USDT\n"
            f"  - **エントリー価格**: <code>{format_price_precision(entry_price)}</code>\n"
            f"  - **決済価格**: <code>{format_price_precision(exit_price)}</code>\n"
            f"  - **PnL (USDT)**: <code>{format_usdt(pnl)}</code> USDT\n"
            f"  - **PnL (%)**: <code>{pnl_percent:+.2f}</code>%\n"
        )
    
    # 1. ヘッダー
    message = (
        f"{trade_status_line}\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"**シンボル**: <b>{symbol}</b> ({timeframe}足)\n"
        f"**スコア**: <code>{score * 100:.2f} / 100</code> (閾値: {current_threshold * 100:.0f} / 100)\n"
        f"**推定勝率**: <code>{estimated_wr}</code>\n"
        f"**指値 (Entry)**: <code>{format_price_precision(entry_price)}</code>\n"
        f"**SL/TP**: <code>{format_price_precision(stop_loss)}</code> / <code>{format_price_precision(take_profit)}</code>\n"
        f"**リスク・リワード**: <code>{rr_ratio:.2f}:1.00</code>\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
    )

    # 2. 取引/決済詳細
    if trade_section:
        message += trade_section + f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"

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
    attempt_count = len(attempt_log) # 分析試行されたが、クールダウンなどでスキップされた銘柄
    
    # 総試行回数 (分析成功数 + スキップ数)
    total_attempts = analyzed_count + attempt_count

    header = (
        f"⏰ **1時間 スコア分析サマリー報告** 📊\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - **期間**: {start_jst} - {now_jst} (JST)\n"
        f"  - **分析試行**: <code>{total_attempts}</code> 銘柄\n"
        f"  - **有効シグナル生成**: <code>{analyzed_count}</code> 銘柄\n"
        f"  - **現在の取引閾値**: <code>{current_threshold*100:.0f} / 100</code>\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
    )
    
    message = header

    # 1. スキップされた銘柄のリスト (最大5つまで)
    if attempt_count > 0:
        skip_list = []
        for symbol, reason in attempt_log.items():
            skip_list.append(f"  - {symbol}: {reason}")
            if len(skip_list) >= 5 and attempt_count > 5:
                 skip_list.append(f"  - ...他 {attempt_count - 5} 銘柄")
                 break

        message += (
            f"\n⚠️ **取引スキップ銘柄 ({attempt_count} 件)**\n"
            f"{'\n'.join(skip_list)}\n"
            f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        )

    # 2. トップスコア銘柄
    if not signals_sorted:
        message += "\nℹ️ この1時間で有効なシグナルはありませんでした。\n"
        
    else:
        best_signal = signals_sorted[0]
        message += (
            f"\n"
            f"🟢 **ベストスコア銘柄 (Top)**\n"
            f"  - **銘柄**: <b>{best_signal['symbol']}</b> ({best_signal['timeframe']})\n"
            f"  - **スコア**: <code>{best_signal['score'] * 100:.2f} / 100</code>\n"
            f"  - **推定勝率**: <code>{get_estimated_win_rate(best_signal['score'])}</code>\n"
            f"  - **指値 (Entry)**: <code>{format_price_precision(best_signal['entry_price'])}</code>\n"
            f"  - **SL/TP**: <code>{format_price_precision(best_signal['stop_loss'])}</code> / <code>{format_price_precision(best_signal['take_profit'])}</code>\n"
        )
        
        # 3. ワーストスコア銘柄 (最低スコアが閾値より低い場合のみ表示)
        worst_signal = signals_sorted[-1]
        if worst_signal['score'] < current_threshold:
             message += (
                f"\n"
                f"🔴 **ワーストスコア銘柄 (Bottom)**\n"
                f"  - **銘柄**: <b>{worst_signal['symbol']}</b> ({worst_signal['timeframe']})\n"
                f"  - **スコア**: <code>{worst_signal['score'] * 100:.2f} / 100</code>\n"
                f"  - **推定勝率**: <code>{get_estimated_win_rate(worst_signal['score'])}</code>\n"
                f"  - **指値 (Entry)**: <code>{format_price_precision(worst_signal['entry_price'])}</code>\n"
                f"  - **SL/TP**: <code>{format_price_precision(worst_signal['stop_loss'])}</code> / <code>{format_price_precision(worst_signal['take_profit'])}</code>\n"
                f"\n"
             )

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
        'data': _to_json_compatible(signal)
    }
    # ログファイルのパスを動的に決定
    log_dir = os.path.join(os.getcwd(), 'logs')
    os.makedirs(log_dir, exist_ok=True)
    
    # 銘柄名とコンテキストをファイル名に含める
    symbol_safe = signal.get('symbol', 'NO_SYMBOL').replace('/', '_')
    log_file = os.path.join(log_dir, f"{symbol_safe}_{context.replace(' ', '_')}.jsonl")
    
    try:
        with open(log_file, 'a') as f:
            f.write(json.dumps(log_data, ensure_ascii=False) + '\n')
    except Exception as e:
        logging.error(f"❌ シグナルログの書き込みに失敗しました: {e}")

# ====================================================================================
# EXCHANGE CLIENT & ACCOUNT FUNCTIONS
# ====================================================================================

async def initialize_exchange_client():
    """CCXTクライアントを初期化する"""
    global EXCHANGE_CLIENT, IS_CLIENT_READY
    
    if IS_CLIENT_READY:
        logging.info("✅ CCXTクライアントは既に初期化されています。スキップします。")
        return

    try:
        # クライアント名に基づいて動的にクラスを決定
        exchange_class = getattr(ccxt_async, CCXT_CLIENT_NAME.lower())
        
        # クライアントのインスタンス化
        EXCHANGE_CLIENT = exchange_class({
            'apiKey': API_KEY,
            'secret': SECRET_KEY,
            'enableRateLimit': True, # レート制限を有効にする
            'options': {
                'defaultType': 'spot', # 現物取引に設定
                # その他の取引所固有のオプション (必要に応じて追加)
            }
        })
        
        # ロードマーケットは初回のみ実行
        await EXCHANGE_CLIENT.load_markets()
        
        # 認証テスト
        if not TEST_MODE:
            balance = await EXCHANGE_CLIENT.fetch_balance()
            logging.info(f"✅ CCXTクライアント ({CCXT_CLIENT_NAME.upper()}) の初期化と認証に成功しました。USDT残高: {format_usdt(balance['USDT']['free'] if 'USDT' in balance else 0.0)}")
        else:
             logging.info(f"✅ CCXTクライアント ({CCXT_CLIENT_NAME.upper()}) の初期化に成功しました。（TEST_MODEのため認証スキップ）")

        IS_CLIENT_READY = True

    except ccxt.AuthenticationError:
        logging.critical("❌ 認証情報が無効です。APIキーとシークレットを確認してください。")
        sys.exit(1)
    except ccxt.ExchangeNotAvailable as e:
        logging.critical(f"❌ 取引所 ({CCXT_CLIENT_NAME.upper()}) が利用できません: {e}")
        sys.exit(1)
    except Exception as e:
        logging.critical(f"❌ CCXTクライアントの初期化中に致命的なエラーが発生: {e}")
        sys.exit(1)

async def fetch_account_status() -> Dict:
    """口座のUSDT残高と総資産額、現物保有資産を取得する"""
    global EXCHANGE_CLIENT, GLOBAL_TOTAL_EQUITY
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        return {'total_usdt_balance': 0.0, 'total_equity': 0.0, 'open_positions': [], 'error': True}
    
    try:
        # 1. 残高の取得
        balance = await EXCHANGE_CLIENT.fetch_balance()
        
        # 2. USDT残高 (自由に使用できる残高)
        total_usdt_balance = balance.get('USDT', {}).get('free', 0.0)
        
        # 3. 総資産額 (Equity)
        # 基本的には全資産の評価額 total['USDT'] などを使用するが、CCXTで総資産を取得できない場合があるため、
        # USDT以外の保有資産の評価も行う
        
        # 暫定的に USDT残高を Equity のベースとする
        total_equity = total_usdt_balance 
        
        # USDT以外の保有資産の評価
        open_positions = []
        for currency, amount_dict in balance.items():
             # 'total' または 'free' を使用するが、ここでは総資産計算のため 'total' を使用
             amount = amount_dict.get('total', 0.0) 
             if currency not in ['USDT', 'USD'] and amount is not None and amount > 0.000001:
                try:
                    symbol = f"{currency}/USDT" 
                    # シンボルが取引所に存在するか確認し、存在しない場合はハイフンなしの形式も試す
                    if symbol not in EXCHANGE_CLIENT.markets:
                        # ccxtでは通常 'ETH/USDT'形式だが、取引所によっては 'ETHUSDT' もありえるため
                        alt_symbol = f"{currency}USDT" 
                        if alt_symbol in EXCHANGE_CLIENT.markets:
                             symbol = alt_symbol
                        else:
                             continue # 取引所にない銘柄は無視

                    ticker = await EXCHANGE_CLIENT.fetch_ticker(symbol)
                    usdt_value = amount * ticker['last']
                    
                    if usdt_value >= 10: # 10 USDT未満の保有は無視
                         open_positions.append({
                             'symbol': symbol,
                             'amount': amount,
                             'usdt_value': usdt_value
                         })
                         total_equity += usdt_value # Equity に加算

                except Exception as e:
                    logging.warning(f"⚠️ {currency} のUSDT価値を取得できませんでした（{EXCHANGE_CLIENT.name} GET {symbol}）。")
        
        # 総資産額をグローバル変数に格納
        GLOBAL_TOTAL_EQUITY = total_equity
        logging.info(f"✅ 口座ステータス取得成功: Equity={format_usdt(GLOBAL_TOTAL_EQUITY)} USDT, Free USDT={format_usdt(total_usdt_balance)}")

        return {
            'total_usdt_balance': total_usdt_balance,
            'total_equity': GLOBAL_TOTAL_EQUITY,
            'open_positions': open_positions,
            'error': False
        }
        
    except Exception as e:
        logging.error(f"❌ 口座ステータス取得中にエラーが発生: {e}", exc_info=True)
        return {'total_usdt_balance': 0.0, 'total_equity': 0.0, 'open_positions': [], 'error': True}

# ====================================================================================
# MARKET & DATA FUNCTIONS
# ====================================================================================

async def fetch_top_symbols() -> List[str]:
    """取引所の出来高TOP銘柄を取得し、監視リストを更新する"""
    global EXCHANGE_CLIENT, CURRENT_MONITOR_SYMBOLS
    
    if SKIP_MARKET_UPDATE:
        logging.info("ℹ️ SKIP_MARKET_UPDATEが有効です。デフォルトの監視リストを使用します。")
        return CURRENT_MONITOR_SYMBOLS

    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.warning("⚠️ CCXTクライアントが未初期化のため、出来高ランキングの取得をスキップします。")
        return CURRENT_MONITOR_SYMBOLS
        
    try:
        # 全ティッカーを取得
        tickers = await EXCHANGE_CLIENT.fetch_tickers()
        
        # USDT建ての現物取引ペアにフィルタリングし、出来高順にソート
        usdt_tickers = {
            symbol: data for symbol, data in tickers.items() 
            if (symbol.endswith('/USDT') or symbol.endswith('USDT')) # シンボル形式のバリエーションに対応
            and data and data.get('quoteVolume') is not None
            and data.get('quoteVolume') > 0
            and data.get('info', {}).get('isSpot', True) # 現物取引であること（CCXTのメタデータがあれば）
        }
        
        # quoteVolume (USDT建て出来高) で降順ソート
        sorted_tickers = sorted(
            usdt_tickers.items(), 
            key=lambda item: item[1]['quoteVolume'], 
            reverse=True
        )
        
        # TOP Nのシンボルを取得
        top_symbols = [symbol for symbol, data in sorted_tickers[:TOP_SYMBOL_LIMIT]]
        
        # デフォルトシンボルとマージし、重複を排除（順序は維持しないが、優先度としてTOP銘柄を多く含む）
        unique_symbols = list(set(top_symbols + DEFAULT_SYMBOLS))
        
        logging.info(f"✅ 出来高TOP {len(top_symbols)} 銘柄を取得しました。総監視対象: {len(unique_symbols)} 銘柄。")
        
        # グローバル変数を更新
        CURRENT_MONITOR_SYMBOLS = unique_symbols
        return unique_symbols

    except Exception as e:
        logging.error(f"❌ 出来高ランキングの取得中にエラーが発生: {e}", exc_info=True)
        return CURRENT_MONITOR_SYMBOLS

async def fetch_fgi_data() -> Dict:
    """Fear & Greed Index (FGI) および関連データを取得し、マクロコンテキストを返す"""
    # 実際には外部API (e.g., alternative.me) を叩く必要があるが、ここでは簡略化のためダミーとします
    # 外部APIの呼び出しは CCXT と同様に try-except で囲むべき

    # 外部API呼び出しの例 (ここではコメントアウト)
    # FGI_API_URL = "https://api.alternative.me/fng/?limit=1"
    # try:
    #     response = requests.get(FGI_API_URL, timeout=5)
    #     response.raise_for_status() # HTTPエラーをチェック
    #     data = response.json().get('data', [])
    #     if data:
    #         value = int(data[0]['value']) # FGIの生の値 (0-100)
    #         value_classification = data[0]['value_classification']
    #         
    #         # スコアリング用のプロキシを計算 (-0.50 から +0.50 の範囲に正規化)
    #         # 0-25 (Extreme Fear) -> -0.50 to -0.25
    #         # 25-50 (Fear) -> -0.25 to 0.00
    #         # 50-75 (Greed) -> 0.00 to +0.25
    #         # 75-100 (Extreme Greed) -> +0.25 to +0.50
    #         fgi_proxy = (value - 50) / 100.0
    #         
    #         # その他のマクロ指標 (例: ドルインデックスDXYのトレンド)
    #         # ここではダミー
    #         forex_bonus = random.uniform(-0.01, 0.01) # 短期的な為替影響

    #         logging.info(f"✅ FGIデータ取得成功: {value_classification} ({value})")
    #         return {
    #             'fgi_proxy': fgi_proxy,
    #             'fgi_raw_value': f"{value} ({value_classification})",
    #             'forex_bonus': forex_bonus 
    #         }
    # except Exception as e:
    #     logging.warning(f"⚠️ FGIデータの取得に失敗しました。デフォルト値を使用します: {e}")
        
    # 失敗またはダミーの場合
    default_fgi_proxy = random.uniform(-0.05, 0.05)
    default_forex_bonus = random.uniform(-0.01, 0.01)

    return {
        'fgi_proxy': default_fgi_proxy,
        'fgi_raw_value': 'N/A (Simulated/Default)',
        'forex_bonus': default_forex_bonus
    }


# ====================================================================================
# TECHNICAL ANALYSIS & SCORING
# ====================================================================================

def calculate_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """必要なテクニカル指標をデータフレームに追加する"""
    
    # 1. 移動平均 (SMA)
    df['SMA50'] = ta.sma(df['close'], length=50)
    df['SMA200'] = ta.sma(df['close'], length=LONG_TERM_SMA_LENGTH) 
    
    # 2. RSI (Relative Strength Index)
    df['RSI'] = ta.rsi(df['close'], length=14)
    
    # 3. MACD (Moving Average Convergence Divergence)
    macd_data = ta.macd(df['close'], fast=12, slow=26, signal=9)
    # ta.macd は複数のカラムを返すため、適切なキーでマージ
    df['MACD'] = macd_data['MACD_12_26_9']
    df['MACDh'] = macd_data['MACDh_12_26_9']
    df['MACDs'] = macd_data['MACDs_12_26_9']
    
    # 4. ATR (Average True Range)
    df['ATR'] = ta.atr(df['high'], df['low'], df['close'], length=14)
    
    # 5. ボリンジャーバンド (BBANDS)
    # 💡 【BBANDSキーの修正】 Key 'BBL_20_2.0' not found エラーに対応
    # pandas-ta のバージョンによってキーが変わるため、標準のキーを使用
    bb_data = ta.bbands(df['close'], length=20, std=2.0, mamode='sma')
    df['BBL'] = bb_data['BBL_20_2.0']
    df['BBM'] = bb_data['BBM_20_2.0']
    df['BBU'] = bb_data['BBU_20_2.0']
    df['BBB'] = bb_data['BBB_20_2.0']

    # 6. OBV (On-Balance Volume)
    df['OBV'] = ta.obv(df['close'], df['volume'])
    df['OBV_SMA'] = ta.sma(df['OBV'], length=20)
    
    # 7. Volume SMA (出来高の平均)
    df['Volume_SMA20'] = ta.sma(df['volume'], length=20)

    # 8. ピボットポイント (Pivots)
    # 終値をピボットポイントの計算に使用
    # Classic Pivot Point (CCXTの提供する情報を使用しないため、ここでは計算しない)

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
    
    # 1. データの有効性チェック
    # SMA200の計算には最低200本必要。ATR/BBandsにもデータが必要。
    required_length = max(REQUIRED_OHLCV_LIMITS.values())
    if len(df) < required_length:
        logging.warning(f"⚠️ {market_ticker['symbol']} ({timeframe}) のデータが {len(df)} 本しかありません。分析をスキップします。")
        return None

    last_candle = df.iloc[-1]
    last_close = last_candle['close']
    
    # 2. 基本的な取引価格の決定 (ATRに基づくSL/TPの設定)
    atr = last_candle['ATR']
    if pd.isna(atr) or atr <= 0:
        logging.warning(f"⚠️ {market_ticker['symbol']} ({timeframe}) のATRが無効です。シグナル生成をスキップします。")
        return None
        
    # ATRに基づく SL/TP の計算 (例: リスク1ATR, リワード1.5ATR)
    # ロットサイズ調整のため、ここでは最小限のリスクリワードを定義
    
    # 買いシグナル (ロング) の場合
    # Entry: 現在価格 (Last Close)
    entry_price = last_close 
    
    # Stop Loss (SL): Entryから1.0 ATR下
    stop_loss = entry_price - (atr * 1.0)
    
    # Take Profit (TP): Entryから1.5 ATR上
    take_profit = entry_price + (atr * 1.5)
    
    # リスク・リワード比の計算 (ここでは1.5:1を仮定)
    rr_ratio = 1.5 
    
    # 価格が0以下になることはありえないため、チェック
    if stop_loss <= 0:
        stop_loss = entry_price * 0.99 # 最低でも1%は下げる

    # 3. スコアリング
    total_score = BASE_SCORE 
    tech_data = {'base_score': BASE_SCORE}
    
    # A. 長期トレンドフィルタ (SMA200との乖離ペナルティ)
    sma200 = last_candle['SMA200']
    long_term_reversal_penalty_value = 0.0
    
    if last_close < sma200:
        # 価格がSMA200より下にある場合、乖離度に応じてペナルティ
        price_diff_percent = (sma200 - last_close) / sma200 
        
        # 乖離率に応じてペナルティを適用 (最大 LONG_TERM_REVERSAL_PENALTY)
        # 例: 5%乖離で最大ペナルティ
        penalty_factor = min(price_diff_percent / 0.05, 1.0)
        long_term_reversal_penalty_value = LONG_TERM_REVERSAL_PENALTY * penalty_factor
        
        total_score -= long_term_reversal_penalty_value
        
    tech_data['long_term_reversal_penalty_value'] = long_term_reversal_penalty_value
    
    # B. 中期/長期トレンドアライメントボーナス (SMA50 > SMA200)
    trend_alignment_bonus_value = 0.0
    sma50 = last_candle['SMA50']
    if sma50 > sma200:
        trend_alignment_bonus_value = TREND_ALIGNMENT_BONUS
        total_score += trend_alignment_bonus_value
        
    tech_data['trend_alignment_bonus_value'] = trend_alignment_bonus_value
    
    # C. 価格構造/ピボット支持ボーナス (ここでは直前の安値 (Low) がSLより十分上にあることを確認)
    structural_pivot_bonus = 0.0
    # 過去5本のローソク足の最低値 (df.iloc[-6:-1]['low']) がSLより上にある
    if len(df) >= 6 and (df.iloc[-6:-1]['low'].min() > stop_loss):
        structural_pivot_bonus = STRUCTURAL_PIVOT_BONUS
        total_score += structural_pivot_bonus
        
    tech_data['structural_pivot_bonus'] = structural_pivot_bonus
    
    # D. MACDペナルティ (MACDヒストグラムがマイナス/MACD線がシグナル線の下)
    macd_penalty_value = 0.0
    if last_candle['MACDh'] < 0 and last_candle['MACD'] < last_candle['MACDs']:
        # MACDがシグナル線の下で、ヒストグラムがマイナス（下落トレンドまたは勢い減速）
        macd_penalty_value = MACD_CROSS_PENALTY
        total_score -= macd_penalty_value

    tech_data['macd_penalty_value'] = macd_penalty_value
    
    # E. Volatility Penalty (低ボラティリティ)
    volatility_penalty_value = 0.0
    bb_width_percent = last_candle['BBB'] / 100.0 # BBBはパーセント表記なので100で割る

    if bb_width_percent < VOLATILITY_BB_PENALTY_THRESHOLD: # 例: 1%未満
        volatility_penalty_value = -MACD_CROSS_PENALTY # MACDと同じ重いペナルティを適用
        total_score += volatility_penalty_value # マイナス値なので加算
        
    tech_data['volatility_penalty_value'] = volatility_penalty_value
    
    # F. RSIモメンタムボーナス (50以上で加速)
    rsi = last_candle['RSI']
    tech_data['rsi_value'] = rsi
    rsi_momentum_bonus_value = 0.0
    
    if rsi >= 70.0:
        rsi_momentum_bonus_value = RSI_MOMENTUM_BONUS_MAX
    elif rsi > 50.0:
        # RSI 50から70の間で線形にボーナスを増加させる
        rsi_momentum_bonus_value = RSI_MOMENTUM_BONUS_MAX * ((rsi - 50.0) / 20.0)
        
    total_score += rsi_momentum_bonus_value
    tech_data['rsi_momentum_bonus_value'] = rsi_momentum_bonus_value
    
    # G. OBV Momentum Bonus (OBVがSMAを上抜けている)
    obv_momentum_bonus_value = 0.0
    # 直近でOBVがOBV_SMAを上抜けしたこと
    # -2 (前々足) から -1 (前足/最新足) にかけてのクロスをチェック
    if (last_candle['OBV'] > last_candle['OBV_SMA']) and \
       (df['OBV'].iloc[-2] <= df['OBV_SMA'].iloc[-2]):
        obv_momentum_bonus_value = OBV_MOMENTUM_BONUS
        total_score += obv_momentum_bonus_value
        
    tech_data['obv_momentum_bonus_value'] = obv_momentum_bonus_value
    
    # H. Volume Spike Bonus (出来高が平均の1.5倍)
    volume_increase_bonus_value = 0.0
    if 'Volume_SMA20' in df.columns and last_candle['Volume_SMA20'] > 0 and \
       last_candle['volume'] > last_candle['Volume_SMA20'] * 1.5:
        volume_increase_bonus_value = VOLUME_INCREASE_BONUS
        total_score += volume_increase_bonus_value
        
    tech_data['volume_increase_bonus_value'] = volume_increase_bonus_value
    
    # I. 流動性ボーナス (板の厚み、ここでは出来高の絶対値で代用)
    # quoteVolumeの対数を使って絶対的な流動性ボーナスを計算
    # (market_ticker['quoteVolume'] を使用するべきだが、tickerには24hボリュームしか含まれないことが多い)
    # ここでは、SMA200/50が有効な銘柄にはベースの流動性ボーナスを与える
    liquidity_bonus_value = 0.0
    if market_ticker.get('quoteVolume', 0) > 1000000: # 100万USDT以上の出来高
        # MAXの70%をベースとして与える (0.07 * 0.7 = 0.049)
        liquidity_bonus_value = LIQUIDITY_BONUS_MAX * 0.7 
        total_score += liquidity_bonus_value

    tech_data['liquidity_bonus_value'] = liquidity_bonus_value
    
    # J. マクロ環境ボーナス/ペナルティ
    fgi_proxy = macro_context.get('fgi_proxy', 0.0)
    forex_bonus = macro_context.get('forex_bonus', 0.0)
    
    # FGIと為替のボーナスを合算し、最大/最小値にクリップ
    sentiment_bonus = max(min(fgi_proxy + forex_bonus, FGI_PROXY_BONUS_MAX), -FGI_PROXY_BONUS_MAX)
    total_score += sentiment_bonus
    tech_data['sentiment_fgi_proxy_bonus'] = sentiment_bonus

    # 4. 最終結果の整形
    # スコアは 0.0 から 1.00 の間にクリップ
    final_score = max(0.0, min(total_score, 1.0))

    signal = {
        'id': str(uuid.uuid4()),
        'timestamp': time.time(),
        'symbol': market_ticker['symbol'],
        'timeframe': timeframe,
        'entry_price': entry_price,
        'stop_loss': stop_loss,
        'take_profit': take_profit,
        'rr_ratio': rr_ratio, # リスクリワード比
        'score': final_score,
        'tech_data': tech_data,
        'is_actionable': final_score >= get_current_threshold(macro_context),
    }

    return signal

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

    except ccxt.ExchangeNotAvailable as e:
        logging.warning(f"⚠️ {symbol} ({tf}) は取引所で利用できません: {e}")
        return {'score': 0.0, 'timeframe': tf, 'symbol': symbol, 'entry_price': 0.0, 'stop_loss': 0.0, 'take_profit': 0.0, 'rr_ratio': 0.0, 'tech_data': {}, 'is_actionable': False, 'reason': f'ExchangeNotAvailable'}
    except ccxt.ExchangeError as e:
        # FGIなどのデータがないため、仮でreasonを追加
        reason = f"ExchangeError: {e}"
        logging.warning(f"⚠️ {symbol} ({tf}) 取引所エラー: {e}")
        return {'score': 0.0, 'timeframe': tf, 'symbol': symbol, 'entry_price': 0.0, 'stop_loss': 0.0, 'take_profit': 0.0, 'rr_ratio': 0.0, 'tech_data': {}, 'is_actionable': False, 'reason': reason}
    except Exception as e:
        # FGIなどのデータがないため、仮でreasonを追加
        reason = f"OHLCV Fetch/Indicator Calc Error: {e}"
        logging.error(f"❌ {symbol} ({tf}) OHLCV取得/指標計算エラー: {e}", exc_info=True)
        return {'score': 0.0, 'timeframe': tf, 'symbol': symbol, 'entry_price': 0.0, 'stop_loss': 0.0, 'take_profit': 0.0, 'rr_ratio': 0.0, 'tech_data': {}, 'is_actionable': False, 'reason': reason}

async def analyze_symbol(symbol: str, market_ticker: Dict) -> List[Dict]:
    """指定された銘柄の全てのタイムフレームで分析を実行する"""
    tasks = []
    for tf in TARGET_TIMEFRAMES:
        tasks.append(fetch_ohlcv_and_analyze(symbol, tf, market_ticker))
    
    # 全てのタイムフレームの分析を並行実行
    signals = await asyncio.gather(*tasks)
    
    # Noneでない有効なシグナルのみを返す
    valid_signals = [s for s in signals if s and s.get('score') is not None]
    
    # 失敗した分析を HOURLY_ATTEMPT_LOG に記録 (クールダウン適用外の理由のみ)
    for s in signals:
         if s and s.get('reason'):
             HOURLY_ATTEMPT_LOG[symbol] = s['reason']

    return valid_signals


# ====================================================================================
# TRADING FUNCTIONS (Order Execution)
# ====================================================================================

def calculate_dynamic_lot_size(score: float, account_status: Dict) -> float:
    """スコアと総資産に基づいて動的な取引ロットサイズを計算する"""
    global GLOBAL_TOTAL_EQUITY
    
    base_trade_size = BASE_TRADE_SIZE_USDT
    total_equity = GLOBAL_TOTAL_EQUITY
    
    # 1. 総資産の割合に基づくロットサイズの計算
    if total_equity > base_trade_size:
        min_lot_usdt = total_equity * DYNAMIC_LOT_MIN_PERCENT
        max_lot_usdt = total_equity * DYNAMIC_LOT_MAX_PERCENT
        
        # 2. スコアに基づくロットサイズの動的な調整 (線形補間)
        # スコア DYNAMIC_LOT_SCORE_MAX (0.96) で max_lot_usdt を適用
        # ベースライン (例: 0.83) から DYNAMIC_LOT_SCORE_MAX までで線形に増加
        
        # スコアが閾値未満の場合は、BASE_TRADE_SIZE_USDT を超えない
        if score < SIGNAL_THRESHOLD_ACTIVE:
            return base_trade_size
            
        # 0.83 から 0.96 の範囲で正規化
        normalized_score = max(0.0, min(1.0, (score - SIGNAL_THRESHOLD_ACTIVE) / (DYNAMIC_LOT_SCORE_MAX - SIGNAL_THRESHOLD_ACTIVE)))
        
        # 最小ロットと最大ロットの間で線形補間
        dynamic_lot = min_lot_usdt + (max_lot_usdt - min_lot_usdt) * normalized_score
        
        # 最小取引額BASE_TRADE_SIZE_USDTを下回らないようにする
        final_lot = max(dynamic_lot, base_trade_size)

        logging.info(f"✅ 動的ロット計算: Score={score:.2f} -> Equity:{format_usdt(total_equity)} -> Lot:{format_usdt(final_lot)} USDT")
        return final_lot
    
    else:
        # 総資産が不明または低すぎる場合は、ベースサイズを使用
        logging.warning("⚠️ 総資産がベース取引サイズ未満または不明です。ベースサイズを使用します。")
        return base_trade_size

async def adjust_order_amount(symbol: str, usdt_amount: float, price: float) -> Tuple[float, float]:
    """USDT建ての希望額を取引所の最小数量/精度に丸める"""
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT.markets or symbol not in EXCHANGE_CLIENT.markets:
        logging.error(f"❌ {symbol} の取引所情報を取得できません。")
        return 0.0, 0.0

    market = EXCHANGE_CLIENT.markets[symbol]
    
    # 1. 注文数量 (ベース通貨) の概算
    base_amount_unrounded = usdt_amount / price
    
    # 2. 数量の精度と最小数量の取得
    amount_precision = market['precision']['amount'] if market and market['precision'] else 8 # デフォルト8桁 (小数点以下の桁数)
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

async def place_sl_tp_orders(
    symbol: str, 
    filled_amount: float, 
    stop_loss: float, 
    take_profit: float
) -> Dict:
    """
    現物ポジションのストップロス (SL) とテイクプロフィット (TP) 注文を同時に設定する。
    Returns: {'status': 'ok', 'sl_order_id': '...', 'tp_order_id': '...'} または {'status': 'error', 'error_message': '...'}
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
        
        tp_order = await EXCHANGE_CLIENT.create_order(
            symbol=symbol,
            type='limit',
            side='sell',
            amount=amount_to_sell,
            price=take_profit,
            params={} # 必要に応じて timeInForce などを設定
        )
        tp_order_id = tp_order.get('id', 'N/A')
        logging.info(f"✅ TP注文 (Limit Sell) 成功: ID={tp_order_id}")

    except Exception as e:
        logging.error(f"❌ TP注文設定中にエラーが発生 ({symbol}): {e}")
        # SL注文をキャンセルするロジックは不要 (まだ発注していないため)
        return {'status': 'error', 'error_message': f'TP注文設定失敗: {e}'}

    # 2. SL (ストップロス) ストップリミット売り注文の設定
    try:
        # CCXTには 'stop_loss' または 'take_profit' という統一された機能はないため、
        # 取引所固有のストップリミット注文機能を使用する (ここでは一般的なパラメーターを想定)
        
        # ストップ価格: stop_loss, リミット価格: stop_loss * 0.99 (スリッページを考慮)
        limit_price_for_sl = stop_loss * 0.99
        
        # 多くの取引所では、create_order の params でトリガー価格を指定する必要がある
        params = {
             'stopPrice': stop_loss, # トリガー価格
             # 他にも 'triggerPrice', 'priceType' などを取引所に応じて設定
        }

        sl_order = await EXCHANGE_CLIENT.create_order(
            symbol=symbol,
            type='limit',
            side='sell',
            amount=amount_to_sell,
            price=limit_price_for_sl, # 実際の指値価格
            params=params, 
            stopLossPrice=stop_loss # CCXTの統一インターフェース (存在する場合)
        )
        sl_order_id = sl_order.get('id', 'N/A')
        logging.info(f"✅ SL注文 (Stop Limit Sell) 成功: ID={sl_order_id}")
        
    except Exception as e:
        logging.error(f"❌ SL注文設定中にエラーが発生 ({symbol}): {e}")
        # TP注文が成功しているため、TP注文をキャンセルする
        if tp_order_id and tp_order_id != 'N/A':
            await cancel_single_order(tp_order_id, symbol)
            
        return {'status': 'error', 'error_message': f'SL注文設定失敗: {e}'}

    return {
        'status': 'ok', 
        'sl_order_id': sl_order_id, 
        'tp_order_id': tp_order_id
    }

async def cancel_single_order(order_id: str, symbol: str) -> bool:
    """単一の注文をキャンセルする"""
    global EXCHANGE_CLIENT
    
    if order_id in [None, 'N/A']:
        logging.info(f"ℹ️ {symbol} 注文IDがありません。キャンセルをスキップします。")
        return True
        
    try:
        await EXCHANGE_CLIENT.cancel_order(order_id, symbol)
        logging.info(f"✅ {symbol} の注文 (ID: {order_id}) をキャンセルしました。")
        return True
    except ccxt.OrderNotFound:
        logging.info(f"ℹ️ {symbol} の注文 (ID: {order_id}) は既に見つかりません/キャンセル済みです。")
        return True
    except Exception as e:
        logging.error(f"❌ {symbol} の注文 (ID: {order_id}) のキャンセルに失敗: {e}")
        return False

async def get_open_orders(symbol: str) -> List[Dict]:
    """特定の銘柄のオープン注文を取得する"""
    global EXCHANGE_CLIENT
    try:
        # CCXTの fetch_open_orders は、未約定の注文を全て返す
        orders = await EXCHANGE_CLIENT.fetch_open_orders(symbol)
        return orders
    except Exception as e:
        logging.error(f"❌ {symbol} のオープン注文取得中にエラーが発生: {e}")
        return []

async def close_position_immediately(symbol: str, amount: float) -> Dict:
    """ポジションを成行売りで強制的にクローズする"""
    global EXCHANGE_CLIENT
    try:
        logging.warning(f"⚠️ {symbol} のポジションを成行売りで強制クローズします (Qty: {amount:.4f})。")
        
        # 成行売り注文
        close_order = await EXCHANGE_CLIENT.create_order(
            symbol=symbol,
            type='market',
            side='sell',
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
    global EXCHANGE_CLIENT, OPEN_POSITIONS
    symbol = signal['symbol']
    entry_price = signal['entry_price']
    stop_loss = signal['stop_loss']
    take_profit = signal['take_profit']
    
    # 1. 動的ロットサイズの計算
    lot_size_usdt = calculate_dynamic_lot_size(signal['score'], account_status)
    signal['lot_size_usdt'] = lot_size_usdt # シグナルデータにロットサイズを保存

    # 2. 注文数量の調整
    # 注文価格: entry_price (シグナルで決定した指値価格)
    base_amount_to_buy, final_usdt_amount = await adjust_order_amount(symbol, lot_size_usdt, entry_price)

    if base_amount_to_buy <= 0.0:
        return {'status': 'error', 'error_message': '調整後の数量が取引所の最小要件を満たしません。'}
        
    logging.info(f"⏳ 現物指値買い注文を発注中: {symbol} @ {format_price_precision(entry_price)} (Qty: {base_amount_to_buy:.4f})")
    
    order_id = 'N/A'
    filled_amount = 0.0
    filled_usdt = 0.0
    
    try:
        # 3. IOC指値買い注文の発注
        # type='limit' と timeInForce: 'IOC' を使用して、即時約定分のみを執行
        params = {'timeInForce': 'IOC'}
        
        order = await EXCHANGE_CLIENT.create_order(
            symbol=symbol,
            type='limit', # 指値注文
            side='buy',
            amount=base_amount_to_buy,
            price=entry_price,
            params=params,
        )
        
        order_id = order.get('id', 'N/A')
        filled_amount = order.get('filled', 0.0)
        filled_usdt = order.get('cost', 0.0) # 約定コスト (USDT)
        
        # 4. 約定数量の確認
        if filled_amount > 0.0:
            logging.info(f"✅ IOC注文約定成功: {symbol} - Qty: {filled_amount:.4f}, Cost: {format_usdt(filled_usdt)} USDT")
            
            # 5. SL/TP注文の発注
            sl_tp_result = await place_sl_tp_orders(
                symbol=symbol,
                filled_amount=filled_amount,
                stop_loss=stop_loss,
                take_profit=take_profit
            )
            
            if sl_tp_result['status'] == 'ok':
                # ポジション管理リストに追加
                new_position = {
                    'id': order_id, # 注文IDをポジションIDとして使用
                    'symbol': symbol,
                    'entry_price': entry_price,
                    'filled_amount': filled_amount,
                    'filled_usdt': filled_usdt,
                    'stop_loss': stop_loss,
                    'take_profit': take_profit,
                    'sl_order_id': sl_tp_result['sl_order_id'],
                    'tp_order_id': sl_tp_result['tp_order_id'],
                    'open_timestamp': time.time(),
                }
                OPEN_POSITIONS.append(new_position)
                
                return {
                    'status': 'ok',
                    'order_id': order_id,
                    'entry_price': entry_price,
                    'filled_amount': filled_amount,
                    'filled_usdt': filled_usdt,
                    'sl_order_id': sl_tp_result['sl_order_id'],
                    'tp_order_id': sl_tp_result['tp_order_id'],
                }
            else:
                # 6. SL/TP設定失敗: 強制クローズ
                logging.error(f"❌ SL/TP設定失敗: {sl_tp_result['error_message']}。ポジションを強制クローズします。")
                close_result = await close_position_immediately(symbol, filled_amount)
                
                return {
                    'status': 'error',
                    'order_id': order_id,
                    'entry_price': entry_price,
                    'filled_amount': filled_amount,
                    'filled_usdt': filled_usdt,
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
            'order_id': order_id,
            'entry_price': entry_price,
            'filled_amount': filled_amount_unknown,
            'filled_usdt': filled_amount_unknown * entry_price, # 概算
            'error_message': f'取引所エラー発生（約定後の可能性あり）: {e}',
            'close_status': close_result['status'],
            'closed_amount': close_result.get('closed_amount', 0.0),
            'close_error_message': close_result.get('error_message'),
        }


# ====================================================================================
# ORDER MANAGEMENT LOOP
# ====================================================================================

async def open_order_management_loop():
    """オープン中のポジション (SL/TP注文) を監視し、決済されたものを削除する"""
    global OPEN_POSITIONS
    
    if not OPEN_POSITIONS:
        logging.debug("ℹ️ オープンポジションはありません。監視をスキップします。")
        return

    positions_to_remove_ids = []
    
    # 全ポジションをイテレート
    for position in OPEN_POSITIONS:
        symbol = position['symbol']
        sl_order_id = position['sl_order_id']
        tp_order_id = position['tp_order_id']
        
        try:
            # 1. オープン注文の確認 (SLとTPの注文)
            open_orders = await get_open_orders(symbol)
            open_order_ids = [order['id'] for order in open_orders]
            
            sl_open = sl_order_id in open_order_ids
            tp_open = tp_order_id in open_order_ids
            
            is_closed = False
            exit_type = None

            if not sl_open and not tp_open:
                # 1. 両方の決済注文が消滅 (SLまたはTPが約定した可能性が高い)
                is_closed = True
                exit_type = "取引所決済完了" 
                logging.info(f"🔴 決済検出: {position['symbol']} - SL/TP注文が取引所から消滅。決済完了と見なします。")

            # 💡 V19.0.53 修正: 決済注文の不完全検出と再設定
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
                
                # 決済通知の送信 (ここでは簡易的にP&Lを0とするか、外部で取得する)
                # 実際の決済価格とPnLは、取引所の注文履歴API (fetchClosedOrders/fetchMyTrades) から取得すべき
                
                # PnL計算の簡略化 (実際は取引履歴から正確な値を取得)
                closed_result = {
                    'symbol': symbol,
                    'entry_price': position['entry_price'],
                    'exit_price': 0.0, # 未知のため0.0 (本来は取引履歴から取得)
                    'filled_amount': position['filled_amount'],
                    'filled_usdt': position['filled_usdt'],
                    'pnl': 0.0,
                    'pnl_percent': 0.0,
                }
                
                # 通知
                notification_message = format_telegram_message(closed_result, "ポジション決済", get_current_threshold(GLOBAL_MACRO_CONTEXT), exit_type=exit_type)
                await send_telegram_message(notification_message)
                log_signal(closed_result, "決済完了")

        except Exception as e:
            logging.error(f"❌ {symbol} の注文監視中にエラーが発生: {e}", exc_info=True)
            # エラーが発生した場合でも、他のポジションの処理を続行

    # 監視が完了したポジションをリストから削除
    OPEN_POSITIONS = [p for p in OPEN_POSITIONS if p['id'] not in positions_to_remove_ids]


# ====================================================================================
# TELEGRAM NOTIFICATION
# ====================================================================================

async def send_telegram_message(message: str):
    """Telegramにメッセージを送信する"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.warning("⚠️ TelegramトークンまたはチャットIDが設定されていません。通知をスキップします。")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    
    # MarkdownV2 または HTML で整形
    payload = {
        'chat_id': TELEGRAM_CHAT_ID,
        'text': message,
        'parse_mode': 'HTML' # HTMLタグを使用
    }
    
    try:
        # HTTPリクエストを同期的に実行 (asyncio.to_threadを使用)
        response = await asyncio.to_thread(requests.post, url, data=payload, timeout=10)
        response.raise_for_status()
        logging.info("✅ Telegramメッセージを送信しました。")
    except Exception as e:
        logging.error(f"❌ Telegram通知の送信に失敗しました: {e}")


# ====================================================================================
# MAIN BOT LOGIC
# ====================================================================================

async def main_bot_loop():
    """ボットのメイン実行ループ (1分ごと)"""
    global LAST_SUCCESS_TIME, LAST_SIGNAL_TIME, LAST_ANALYSIS_SIGNALS, CURRENT_MONITOR_SYMBOLS, GLOBAL_MACRO_CONTEXT, LAST_HOURLY_NOTIFICATION_TIME, IS_FIRST_MAIN_LOOP_COMPLETED, HOURLY_SIGNAL_LOG, HOURLY_ATTEMPT_LOG, BOT_VERSION
    
    while True:
        start_time = time.time()
        now_jst = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")
        logging.info(f"--- 💡 {now_jst} - BOT LOOP START (M1 Frequency) ---")

        try:
            # 1. FGIデータを取得し、グローバルマクロコンテキストを更新
            GLOBAL_MACRO_CONTEXT = await fetch_fgi_data() 
            current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)

            # 2. 口座ステータスを取得
            account_status = await fetch_account_status()
            
            # 初回起動通知
            if not IS_FIRST_MAIN_LOOP_COMPLETED:
                startup_message = format_startup_message(
                    account_status, 
                    GLOBAL_MACRO_CONTEXT, 
                    len(CURRENT_MONITOR_SYMBOLS),
                    current_threshold,
                    BOT_VERSION
                )
                await send_telegram_message(startup_message)
                IS_FIRST_MAIN_LOOP_COMPLETED = True
            
            # 3. 出来高ランキングを更新 (LOOP_INTERVALごとに1回更新されるように設計)
            if time.time() - LAST_SUCCESS_TIME > LOOP_INTERVAL:
                CURRENT_MONITOR_SYMBOLS = await fetch_top_symbols()
            
            # 4. 全銘柄の分析を並行実行
            market_tickers = {}
            if EXCHANGE_CLIENT and IS_CLIENT_READY:
                # 分析対象銘柄の現在のティッカー価格を取得（流動性ボーナスに使用）
                tickers = await EXCHANGE_CLIENT.fetch_tickers(symbols=CURRENT_MONITOR_SYMBOLS)
                market_tickers = {s: t for s, t in tickers.items() if t is not None}
            
            analysis_tasks = []
            for symbol in CURRENT_MONITOR_SYMBOLS:
                 if symbol in market_tickers:
                     analysis_tasks.append(analyze_symbol(symbol, market_tickers[symbol]))
                 else:
                     logging.warning(f"⚠️ {symbol} のティッカー情報が見つかりません。分析をスキップします。")
                     HOURLY_ATTEMPT_LOG[symbol] = "Ticker Not Found"

            all_signals_nested = await asyncio.gather(*analysis_tasks)
            all_signals = [s for sublist in all_signals_nested for s in sublist]
            
            # 5. ベストシグナルをフィルタリング
            # Score順にソートし、閾値以上、クールダウン対象外のものを選択
            best_signals = sorted(
                [s for s in all_signals if s.get('is_actionable')], 
                key=lambda x: x['score'], 
                reverse=True
            )
            
            # 6. 取引実行ロジック
            trade_result = None
            
            if best_signals:
                best_signal = best_signals[0]
                symbol = best_signal['symbol']
                score = best_signal['score']
                
                # クールダウンチェック (2時間以内ならスキップ)
                last_signal_time = LAST_SIGNAL_TIME.get(symbol, 0.0)
                if time.time() - last_signal_time < TRADE_SIGNAL_COOLDOWN:
                    reason = f"CoolDown ({((TRADE_SIGNAL_COOLDOWN - (time.time() - last_signal_time)) / 60):.0f} min left)"
                    HOURLY_ATTEMPT_LOG[symbol] = reason
                    logging.info(f"ℹ️ {symbol} はクールダウン期間中です。取引をスキップします。")
                
                # USDT残高チェック
                elif account_status['total_usdt_balance'] < MIN_USDT_BALANCE_FOR_TRADE:
                    error_message = f"残高不足 (現在: {format_usdt(account_status['total_usdt_balance'])} USDT)。新規取引に必要な額: {MIN_USDT_BALANCE_FOR_TRADE:.2f} USDT。"
                    trade_result = {'status': 'error', 'error_message': error_message, 'close_status': 'skipped'}
                    logging.warning(f"⚠️ {best_signal['symbol']} 取引スキップ: {error_message}")
                
                # ポジション重複チェック
                elif any(p['symbol'] == symbol for p in OPEN_POSITIONS):
                    reason = "Position Already Open"
                    HOURLY_ATTEMPT_LOG[symbol] = reason
                    logging.info(f"ℹ️ {symbol} は既にオープンポジションがあります。取引をスキップします。")
                
                # 閾値チェック (再チェックだが、念のため)
                elif score >= current_threshold:
                    if not TEST_MODE:
                        # 取引実行
                        trade_result = await execute_trade(best_signal, account_status)
                        
                        if trade_result['status'] == 'ok':
                            LAST_SIGNAL_TIME[symbol] = time.time()
                            log_signal(best_signal, "取引成功シグナル")
                        elif trade_result['close_status'] != 'skipped':
                            # SL/TP失敗で強制クローズした場合も記録
                            log_signal(best_signal, "取引失敗_強制クローズ")
                        else:
                             # 単純な約定失敗（IOCスキップ）は、クールダウンをリセットしない
                             log_signal(best_signal, "取引失敗_スキップ")
                    else:
                        trade_result = {'status': 'ok_test', 'entry_price': best_signal['entry_price'], 'filled_amount': 0.0, 'filled_usdt': 0.0}
                        log_signal(best_signal, "取引シグナル_テストモード")
                        
                    # 7. Telegram通知
                    notification_message = format_telegram_message(best_signal, "取引シグナル", current_threshold, trade_result)
                    await send_telegram_message(notification_message)
                    
                else:
                    logging.info(f"ℹ️ {best_signal['symbol']} は閾値 {current_threshold*100:.2f} を満たしていません。取引をスキップします。")

            # 8. 1時間ごとのレポート
            HOURLY_SIGNAL_LOG.extend([s for s in all_signals if s.get('score') is not None])
            
            if time.time() - LAST_HOURLY_NOTIFICATION_TIME >= HOURLY_SCORE_REPORT_INTERVAL:
                if HOURLY_SIGNAL_LOG:
                    # ログの分析開始時間を取得 (最初のレコードのタイムスタンプを使用)
                    log_start_time = min(s.get('timestamp', time.time()) for s in HOURLY_SIGNAL_LOG)
                    
                    report_message = format_hourly_report(
                        HOURLY_SIGNAL_LOG, 
                        HOURLY_ATTEMPT_LOG,
                        log_start_time,
                        current_threshold,
                        BOT_VERSION
                    )
                    await send_telegram_message(report_message)
                
                # ログをクリアし、通知時刻を更新
                HOURLY_SIGNAL_LOG = []
                HOURLY_ATTEMPT_LOG = {}
                LAST_HOURLY_NOTIFICATION_TIME = time.time()


            # 成功時刻を更新
            LAST_SUCCESS_TIME = time.time()
            
        except Exception as e:
            logging.critical(f"❌ メインループ中に致命的なエラーが発生: {e}", exc_info=True)
            # 致命的なエラー発生時にTelegramに通知
            error_msg = f"🚨 **FATAL ERROR** - メインループで致命的なエラーが発生しました。\n<pre>{e.__class__.__name__}: {str(e)[:200]}...</pre>"
            try:
                await send_telegram_message(f"{error_msg}\n\n<i>Bot Ver: {BOT_VERSION}</i>")
            except Exception as notify_e:
                 logging.error(f"❌ 致命的エラー通知の送信に失敗: {notify_e}")

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
    
    # メインBOTタスクの開始
    asyncio.create_task(main_bot_loop())
    
    # 注文監視タスクの開始
    asyncio.create_task(open_order_management_scheduler())

@app.get("/status")
async def get_status():
    """ボットの現在の状態を返すエンドポイント"""
    return JSONResponse(content={
        "status": "running" if IS_CLIENT_READY else "initializing",
        "version": BOT_VERSION,
        "test_mode": TEST_MODE,
        "exchange": CCXT_CLIENT_NAME.upper(),
        "total_equity": GLOBAL_TOTAL_EQUITY,
        "open_positions_count": len(OPEN_POSITIONS),
        "monitor_symbols_count": len(CURRENT_MONITOR_SYMBOLS),
        "last_success_time": datetime.fromtimestamp(LAST_SUCCESS_TIME, JST).strftime("%Y/%m/%d %H:%M:%S") if LAST_SUCCESS_TIME else "N/A"
    })

@app.get("/open_positions")
async def get_open_positions():
    """オープン中のポジションリストを返すエンドポイント"""
    return JSONResponse(content=OPEN_POSITIONS)


if __name__ == "__main__":
    # uvicornの起動
    # ホスト '0.0.0.0' で外部からのアクセスを許可
    uvicorn.run(app, host="0.0.0.0", port=8000)
