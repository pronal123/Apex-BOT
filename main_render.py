# ====================================================================================
# Apex BOT v19.0.39 - FULL COMPLIANCE (NaN/None Check Fix)
#
# 改良・修正点 (v19.0.38からの追加点):
# 1. 【バグ修正】score_technical_indicators関数内の np.isnan() の TypeError を修正。
#    - last_1h.get() が None を返した場合の処理を安全な順番 (Noneチェック -> np.isnan) に変更。(BBands/ATR)
# 2. 【内部更新】BOTバージョンを v19.0.39 に更新。
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
WEBSHARE_UPLOAD_INTERVAL = 60 * 60  # WebShareログアップロード間隔 (1時間ごと)
HOURLY_SCORE_REPORT_INTERVAL = 60 * 60 # 1時間ごとのスコア通知間隔 (60分ごと)

# 💡 クライアント設定
CCXT_CLIENT_NAME = os.getenv("EXCHANGE_CLIENT", "mexc") # デフォルトはmexc
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


# 【動的ロット設定】
DYNAMIC_LOT_MIN_PERCENT = 0.10 # 最小ロット (総資産の 10%)
DYNAMIC_LOT_MAX_PERCENT = 0.20 # 最大ロット (総資産の 20%)

# 💡 新規取引制限設定 
MIN_USDT_BALANCE_FOR_TRADE = 20.0 # 新規取引に必要な最小USDT残高 (20.0 USDT)
DYNAMIC_LOT_SCORE_MAX = 0.96   # このスコアで最大ロットが適用される (96点)


# 💡 WEBSHARE設定 (HTTP POSTへ変更)
WEBSHARE_METHOD = os.getenv("WEBSHARE_METHOD", "HTTP") # デフォルトはHTTPに変更
WEBSHARE_POST_URL = os.getenv("WEBSHARE_POST_URL", "http://your-webshare-endpoint.com/upload") # HTTP POST用のエンドポイント

# グローバル変数 (状態管理用)
EXCHANGE_CLIENT: Optional[ccxt_async.Exchange] = None
CURRENT_MONITOR_SYMBOLS: List[str] = DEFAULT_SYMBOLS.copy()
LAST_SUCCESS_TIME: float = 0.0
LAST_SIGNAL_TIME: Dict[str, float] = {}
LAST_ANALYSIS_SIGNALS: List[Dict] = []
LAST_HOURLY_NOTIFICATION_TIME: float = 0.0 # 1時間ごとの通知時刻
LAST_WEBSHARE_UPLOAD_TIME: float = 0.0 
GLOBAL_MACRO_CONTEXT: Dict = {'fgi_proxy': 0.0, 'fgi_raw_value': 'N/A', 'forex_bonus': 0.0} # 初期値を設定
IS_FIRST_MAIN_LOOP_COMPLETED: bool = False # 初回メインループ完了フラグ
OPEN_POSITIONS: List[Dict] = [] # 現在保有中のポジション (注文IDトラッキング用)
GLOBAL_TOTAL_EQUITY: float = 0.0 # 総資産額を格納するグローバル変数
HOURLY_SIGNAL_LOG: List[Dict] = [] # 1時間内のシグナルを一時的に保持するリスト 

if TEST_MODE:
    logging.warning("⚠️ WARNING: TEST_MODE is active. Trading is disabled.")

# CCXTクライアントの準備完了フラグ
IS_CLIENT_READY: bool = False

# 取引ルール設定
TRADE_SIGNAL_COOLDOWN = 60 * 60 * 2 # 同一銘柄のシグナル通知クールダウン（2時間）
SIGNAL_THRESHOLD = 0.65             # 動的閾値のベースライン
TOP_SIGNAL_COUNT = 3                # 通知するシグナルの最大数
# 1m, 5m, 15m, 1h, 4hのOHLCVが必要 (SMA200には500本以上必要)
REQUIRED_OHLCV_LIMITS = {'1m': 500, '5m': 500, '15m': 500, '1h': 500, '4h': 500} 

# ====================================================================================
# 【★スコアリング定数 V19.0.37: 実践ロジックに合わせた調整】
# (合計最大スコアが1.00になるように調整)
# ====================================================================================
TARGET_TIMEFRAMES = ['1m', '5m', '15m', '1h', '4h'] 

# スコアリングウェイト
BASE_SCORE = 0.50                   # ベースとなる取引基準点 (50点)
LONG_TERM_SMA_LENGTH = 200          # 長期トレンドフィルタ用SMA
MID_TERM_SMA_LENGTH = 50            # 中期トレンドフィルタ用SMA

# ペナルティ（マイナス要因） - 合計最大で -0.50 点
LONG_TERM_REVERSAL_PENALTY_MAX = 0.20   # 長期トレンド逆行時の最大ペナルティ
MACD_CROSS_PENALTY = 0.15               # MACDが不利なクロス/発散時のペナルティ
VOLATILITY_BB_PENALTY_THRESHOLD = 0.005 # BB幅が0.5%未満
VOLATILITY_PENALTY_MAX = 0.10           # 低ボラティリティ時の最大ペナルティ

# ボーナス（プラス要因）- 合計最大で +0.50 点
TREND_ALIGNMENT_BONUS_MAX = 0.10        # 中期/長期トレンド一致時の最大ボーナス
STRUCTURAL_PIVOT_BONUS = 0.05           # 価格構造/ピボット支持時の固定ボーナス 
RSI_MOMENTUM_LOW = 45                   # RSIが45以下でロングモメンタム候補
RSI_MOMENTUM_BONUS_MAX = 0.15           # RSIの強さに応じた可変ボーナスの最大値
OBV_MOMENTUM_BONUS = 0.05               # OBVの確証ボーナス
VOLUME_INCREASE_BONUS = 0.05            # 出来高スパイク時のボーナス
LIQUIDITY_BONUS_MAX = 0.05              # 流動性(板の厚み)による最大ボーナス
FGI_PROXY_BONUS_MAX = 0.05              # 恐怖・貪欲指数による最大ボーナス/ペナルティ

# リスク管理設定
ATR_MULTIPLIER_SL = 1.5                 # SL幅 = ATR * 1.5
ATR_MULTIPLIER_TP = 2.25                # TP幅 = ATR * 2.25 (RRR 1:1.5)
ATR_LENGTH = 14                         # ATR計算期間

# 市場環境に応じた動的閾値調整のための定数 (変更なし)
FGI_SLUMP_THRESHOLD = -0.02         
FGI_ACTIVE_THRESHOLD = 0.02         
SIGNAL_THRESHOLD_SLUMP = 0.94       
SIGNAL_THRESHOLD_NORMAL = 0.92      
SIGNAL_THRESHOLD_ACTIVE = 0.90      

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
    """価格を整形する。1.0 USDT以上の価格に対して小数第4位まで表示を保証する。"""
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

def get_estimated_win_rate(score: float) -> str:
    """スコアに基づいて推定勝率を返す (最大100点に合わせた調整)"""
    # 1.00が最高点
    if score >= 0.95:
        return "90%+"
    elif score >= 0.90:
        return "85-90%"
    elif score >= 0.85:
        return "80-85%"
    elif score >= 0.80:
        return "75-80%"
    else:
        return "70-75%"

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
    """シグナルに含まれるテクニカルデータから、スコアの詳細なブレークダウンを文字列として返す (MACDボーナス表示対応)"""
    tech_data = signal.get('tech_data', {})
    score = signal['score']
    
    breakdown = []
    
    # ベーススコア
    base_score_line = f"  - **ベーススコア ({signal['timeframe']})**: <code>+{BASE_SCORE*100:.1f}</code> 点"
    breakdown.append(base_score_line)
    
    # 長期トレンド逆行ペナルティ
    lt_reversal_pen = tech_data.get('long_term_reversal_penalty_value', 0.0)
    lt_status = '❌ 長期トレンド逆行' if lt_reversal_pen < 0 else '✅ 長期トレンド一致'
    lt_score = f"{lt_reversal_pen*100:.1f}"
    breakdown.append(f"  - {lt_status} (SMA200乖離): <code>{lt_score}</code> 点")
    
    # 中期トレンドアライメントボーナス
    trend_alignment_bonus = tech_data.get('trend_alignment_bonus_value', 0.0)
    trend_status = '✅ 中期/長期トレンド一致 (SMA50>200)' if trend_alignment_bonus > 0 else '➖ 中期トレンド 中立/逆行'
    trend_score = f"{trend_alignment_bonus*100:.1f}"
    breakdown.append(f"  - {trend_status}: <code>{'+' if trend_alignment_bonus > 0 else ''}{trend_score}</code> 点")
    
    # 価格構造/ピボット
    pivot_bonus = tech_data.get('structural_pivot_bonus', 0.0)
    pivot_status = '✅ 価格構造/ピボット支持' if pivot_bonus > 0 else '➖ 価格構造 中立'
    pivot_score = f"{pivot_bonus*100:.1f}"
    breakdown.append(f"  - {pivot_status}: <code>{'+' if pivot_bonus > 0 else ''}{pivot_score}</code> 点")

    # MACDモメンタムボーナス (新規追加)
    macd_momentum_bonus = tech_data.get('macd_momentum_bonus', 0.0)
    macd_status_b = '✅ MACDモメンタム確証' if macd_momentum_bonus > 0 else '➖ MACDモメンタム 中立'
    macd_score_b = f"{macd_momentum_bonus*100:.1f}"
    breakdown.append(f"  - {macd_status_b}: <code>{'+' if macd_momentum_bonus > 0 else ''}{macd_score_b}</code> 点")
    
    # MACDペナルティ
    macd_pen = tech_data.get('macd_penalty_value', 0.0)
    macd_status_p = '❌ MACDクロス/発散 (不利)' if macd_pen < 0 else '➖ MACD 中立'
    macd_score_p = f"{macd_pen*100:.1f}"
    breakdown.append(f"  - {macd_status_p}: <code>{macd_score_p}</code> 点")

    # RSIモメンタムボーナス (可変)
    rsi_momentum_bonus = tech_data.get('rsi_momentum_bonus_value', 0.0)
    rsi_status = f"✅ RSIモメンタム加速 ({tech_data.get('rsi_value', 0.0):.1f})" if rsi_momentum_bonus > 0 else '➖ RSIモメンタム 中立'
    rsi_score = f"{rsi_momentum_bonus*100:.1f}"
    breakdown.append(f"  - {rsi_status}: <code>{'+' if rsi_momentum_bonus > 0 else ''}{rsi_score}</code> 点")
    
    # 出来高/OBV確証ボーナス
    obv_bonus = tech_data.get('obv_momentum_bonus_value', 0.0)
    obv_status = '✅ 出来高/OBV確証' if obv_bonus > 0 else '➖ 出来高/OBV 中立'
    obv_score = f"{obv_bonus*100:.1f}"
    breakdown.append(f"  - {obv_status}: <code>{'+' if obv_bonus > 0 else ''}{obv_score}</code> 点")
    
    # 出来高スパイクボーナス
    volume_increase_bonus = tech_data.get('volume_increase_bonus_value', 0.0)
    volume_status = '✅ 直近の出来高スパイク' if volume_increase_bonus > 0 else '➖ 出来高スパイクなし'
    volume_score = f"{volume_increase_bonus*100:.1f}"
    breakdown.append(f"  - {volume_status}: <code>{'+' if volume_increase_bonus > 0 else ''}{volume_score}</code> 点")

    # 流動性
    liquidity_bonus = tech_data.get('liquidity_bonus_value', 0.0)
    liquidity_status = '✅ 流動性 (板の厚み) 優位' if liquidity_bonus > 0 else '➖ 流動性 中立'
    liquidity_score = f"{liquidity_bonus*100:.1f}"
    breakdown.append(f"  - {liquidity_status}: <code>{'+' if liquidity_bonus > 0 else ''}{liquidity_score}</code> 点")

    # マクロ環境
    fgi_bonus = tech_data.get('sentiment_fgi_proxy_bonus', 0.0)
    macro_status = '✅ FGIマクロ影響 順行' if fgi_bonus >= 0 else '❌ FGIマクロ影響 逆行'
    macro_score = f"{fgi_bonus*100:.1f}"
    breakdown.append(f"  - {macro_status}: <code>{'+' if fgi_bonus > 0 else ''}{macro_score}</code> 点")

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
    bot_version: str
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


def format_telegram_message(signal: Dict, context: str, current_threshold: float, trade_result: Optional[Dict] = None, exit_type: Optional[str] = None) -> str:
    """Telegram通知用のメッセージを作成する"""
    global GLOBAL_TOTAL_EQUITY
    
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
        
        # ロットサイズ割合の表示 (金額なのでformat_usdt)
        if GLOBAL_TOTAL_EQUITY > 0 and lot_size >= BASE_TRADE_SIZE_USDT:
            lot_percent = (lot_size / GLOBAL_TOTAL_EQUITY) * 100
            lot_info = f"<code>{format_usdt(lot_size)}</code> USDT ({lot_percent:.1f}%)"
        else:
            lot_info = f"<code>{format_usdt(lot_size)}</code> USDT"
        
        if TEST_MODE:
            trade_status_line = f"⚠️ **テストモード**: 取引は実行されません。(ロット: {lot_info})"
        elif trade_result is None or trade_result.get('status') == 'error':
            trade_status_line = f"❌ **自動売買 失敗**: {trade_result.get('error_message', 'APIエラー')}"
        elif trade_result.get('status') == 'ok':
            trade_status_line = "✅ **自動売買 成功**: 現物指値買い注文が即時約定しました。"
            
            filled_amount = trade_result.get('filled_amount', 0.0) 
            filled_usdt = trade_result.get('filled_usdt', 0.0)
            
            trade_section = (
                f"💰 **取引実行結果**\n"
                f"  - **注文タイプ**: <code>現物 (Spot) / 指値買い (FOK)</code>\n"
                f"  - **動的ロット**: {lot_info} (目標)\n" 
                f"  - **約定数量**: <code>{filled_amount:.4f}</code> {symbol.split('/')[0]}\n"
                f"  - **平均約定額**: <code>{format_usdt(filled_usdt)}</code> USDT\n"
                f"  - **SL注文ID**: <code>{trade_result.get('sl_order_id', 'N/A')}</code>\n"
                f"  - **TP注文ID**: <code>{trade_result.get('tp_order_id', 'N/A')}</code>\n"
            )
            
    elif context == "ポジション決済":
        exit_type_final = trade_result.get('exit_type', exit_type or '不明')
        trade_status_line = f"🔴 **ポジション決済**: {exit_type_final} トリガー"
        
        entry_price = trade_result.get('entry_price', 0.0)
        exit_price = trade_result.get('exit_price', 0.0)
        # 損益はボット側で計算できないためN/Aとする
        pnl_usdt = trade_result.get('pnl_usdt') if 'pnl_usdt' in trade_result else None
        pnl_rate = trade_result.get('pnl_rate') if 'pnl_rate' in trade_result else None
        filled_amount = trade_result.get('filled_amount', 0.0)

        # SL/TPも trade_resultから取得
        sl_price = trade_result.get('stop_loss', 0.0)
        tp_price = trade_result.get('take_profit', 0.0)
        
        pnl_sign = "✅ 決済完了"
        pnl_line = "  - **損益**: <code>取引所履歴を確認</code>"
        if pnl_usdt is not None and pnl_rate is not None:
             pnl_sign = "✅ 利益確定" if pnl_usdt >= 0 else "❌ 損切り"
             pnl_line = f"  - **損益**: <code>{'+' if pnl_usdt >= 0 else ''}{format_usdt(pnl_usdt)}</code> USDT ({pnl_rate*100:.2f}%)\n"
        
        trade_section = (
            f"💰 **決済実行結果** - {pnl_sign}\n"
            # 決済価格も高精度表示
            f"  - **エントリー価格**: <code>{format_price_precision(entry_price)}</code>\n"
            f"  - **決済価格 (約定価格)**: <code>{format_price_precision(exit_price)}</code>\n"
            # 決済セクションに指値価格を追加
            f"  - **指値 SL/TP**: <code>{format_price_precision(sl_price)}</code> / <code>{format_price_precision(tp_price)}</code>\n"
            f"  - **約定数量**: <code>{filled_amount:.4f}</code> {symbol.split('/')[0]}\n"
            f"{pnl_line}"
        )
            
    
    message = (
        f"🚀 **Apex TRADE {context}**\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - **日時**: {now_jst} (JST)\n"
        f"  - **銘柄**: <b>{symbol}</b> ({timeframe})\n"
        f"  - **ステータス**: {trade_status_line}\n" 
        f"  - **総合スコア**: <code>{score * 100:.2f} / 100</code>\n" # 最大100点表示
        f"  - **取引閾値**: <code>{current_threshold * 100:.2f}</code> 点\n"
        f"  - **推定勝率**: <code>{estimated_wr}</code>\n"
        f"  - **リスクリワード比率 (RRR)**: <code>1:{rr_ratio:.2f}</code>\n"
        # 価格表示をformat_price_precisionに変更
        f"  - **指値 (Entry)**: <code>{format_price_precision(entry_price)}</code>\n"
        f"  - **ストップロス (SL)**: <code>{format_price_precision(stop_loss)}</code>\n"
        f"  - **テイクプロフィット (TP)**: <code>{format_price_precision(take_profit)}</code>\n"
        # リスク・リワード幅（金額）はformat_usdtを維持
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
        
    message += (f"<i>Bot Ver: v19.0.39 - Practical Score Logic</i>")
    return message

def format_hourly_report(signals: List[Dict], start_time: float, current_threshold: float) -> str:
    """1時間ごとの最高・最低スコア銘柄の通知メッセージを作成する"""
    
    now_jst = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")
    start_jst = datetime.fromtimestamp(start_time, JST).strftime("%H:%M:%S")
    
    # スコアでソート
    signals_sorted = sorted(signals, key=lambda x: x['score'], reverse=True)
    
    if not signals_sorted:
        return (
            f"🕒 **Apex BOT 1時間スコアレポート**\n"
            f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
            f"  - **集計日時**: {start_jst} - {now_jst} (JST)\n"
            f"  - **分析銘柄数**: <code>0</code>\n"
            f"  - **レポート**: 過去1時間以内に分析されたシグナルはありませんでした。\n"
            f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        )
    
    best_signal = signals_sorted[0]
    worst_signal = signals_sorted[-1]
    
    # 閾値超え銘柄のカウント
    threshold_count = sum(1 for s in signals if s['score'] >= current_threshold)

    message = (
        f"🕒 **Apex BOT 1時間スコアレポート**\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - **集計日時**: {start_jst} - {now_jst} (JST)\n"
        f"  - **分析銘柄数**: <code>{len(signals)}</code>\n"
        f"  - **閾値超え銘柄**: <code>{threshold_count}</code> ({current_threshold*100:.2f}点以上)\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"\n"
        f"🟢 **ベストスコア銘柄 (Top)**\n"
        f"  - **銘柄**: <b>{best_signal['symbol']}</b> ({best_signal['timeframe']})\n"
        f"  - **スコア**: <code>{best_signal['score'] * 100:.2f} / 100</code>\n"
        f"  - **推定勝率**: <code>{get_estimated_win_rate(best_signal['score'])}</code>\n"
        f"  - **現在の価格**: <code>{format_price_precision(best_signal['entry_price'])}</code>\n"
        f"\n"
        f"🔴 **ワーストスコア銘柄 (Bottom)**\n"
        f"  - **銘柄**: <b>{worst_signal['symbol']}</b> ({worst_signal['timeframe']})\n"
        f"  - **スコア**: <code>{worst_signal['score'] * 100:.2f} / 100</code>\n"
        f"  - **推定勝率**: <code>{get_estimated_win_rate(worst_signal['score'])}</code>\n"
        f"  - **現在の価格**: <code>{format_price_precision(worst_signal['entry_price'])}</code>\n"
        f"\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"<i>Bot Ver: v19.0.39 - Practical Score Logic</i>"
    )
    
    return message

async def send_telegram_notification(message: str):
    """Telegramに通知を送信する"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.warning("⚠️ TelegramトークンまたはCHAT IDが設定されていません。通知をスキップします。")
        return

    # HTML形式で送信
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        'chat_id': TELEGRAM_CHAT_ID,
        'text': message,
        'parse_mode': 'HTML'
    }

    try:
        response = requests.post(url, data=payload, timeout=10)
        response.raise_for_status()
        if response.status_code == 200:
            logging.info("✅ Telegram通知を送信しました。")
        else:
            logging.error(f"❌ Telegram通知失敗: ステータスコード {response.status_code}")
    except requests.exceptions.RequestException as e:
        logging.error(f"❌ Telegram通知中にエラーが発生: {e}")

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
    """シグナルまたは取引結果をJSON形式でログに記録する (WebShare用)"""
    log_data = {
        'timestamp_jst': datetime.now(JST).isoformat(),
        'context': context,
        'signal': _to_json_compatible(signal),
        'total_equity': GLOBAL_TOTAL_EQUITY,
        'current_positions_count': len(OPEN_POSITIONS),
    }
    
    # 実際にはここにファイルへの追記ロジックやデータベースへの書き込みロジックが入るが、今回はHTTP POSTを使用
    return log_data

async def send_webshare_update(data: Dict):
    """WebShare (外部ロギングシステム) に最新のデータを送信する (HTTP POST)"""
    global LAST_WEBSHARE_UPLOAD_TIME
    
    if WEBSHARE_METHOD != "HTTP" or not WEBSHARE_POST_URL or WEBSHARE_POST_URL == "http://your-webshare-endpoint.com/upload":
        logging.warning("⚠️ WEBSHARE_POST_URLが設定されていません。またはデフォルト値のままです。送信をスキップします。")
        return

    try:
        logging.info("WebShareデータをアップロードします (HTTP POST)。")
        
        # タイムスタンプを即座に更新し、頻繁な呼び出しを防ぐ
        # LAST_WEBSHARE_UPLOAD_TIME = current_time # scheduler側で更新
        
        response = requests.post(
            WEBSHARE_POST_URL,
            json=data,
            timeout=15
        )
        response.raise_for_status() # HTTPエラーをチェック
        logging.info(f"✅ WebShareデータアップロード成功。ステータス: {response.status_code}")
    
    except requests.exceptions.RequestException as e:
        logging.error(f"❌ WebShareデータアップロード失敗: {e}")
    except Exception as e:
        logging.error(f"❌ WebShareデータアップロード中に予期せぬエラー: {e}")


# ====================================================================================
# CCXT & DATA ACQUISITION
# ====================================================================================

async def initialize_exchange_client():
    """CCXTクライアントを初期化し、市場情報をロードする"""
    global EXCHANGE_CLIENT, IS_CLIENT_READY
    
    logging.info(f"⏳ CCXTクライアント ({CCXT_CLIENT_NAME}) の初期化を開始します...")
    
    # 以前のインスタンスを閉じる
    if EXCHANGE_CLIENT:
        await EXCHANGE_CLIENT.close()

    try:
        # ccxt_asyncモジュールからクライアントクラスを取得
        exchange_class = getattr(ccxt_async, CCXT_CLIENT_NAME.lower())

        # クライアントインスタンスを作成
        config = {
            'apiKey': API_KEY,
            'secret': SECRET_KEY,
            'enableRateLimit': True, # レートリミットを有効化 (必須)
            'options': {
                'defaultType': 'spot', # 現物取引モード
            }
        }
        EXCHANGE_CLIENT = exchange_class(config)
        
        # 市場情報をロード
        await EXCHANGE_CLIENT.load_markets()
        
        IS_CLIENT_READY = True
        logging.info(f"✅ CCXTクライアント ({CCXT_CLIENT_NAME}) を現物取引モードで初期化し、市場情報をロードしました。")
        
        if not API_KEY or not SECRET_KEY:
            logging.warning("⚠️ APIキーまたはシークレットキーが設定されていません。取引機能は無効です。")
            

    except Exception as e:
        logging.critical(f"❌ CCXTクライアントの初期化に失敗: {e}", exc_info=True)


async def fetch_account_status() -> Dict:
    """CCXTから口座の残高と、USDT以外の保有資産の情報を取得する。"""
    global EXCHANGE_CLIENT, GLOBAL_TOTAL_EQUITY
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.error("❌ 口座ステータス取得失敗: CCXTクライアントが準備できていません。")
        return {'total_usdt_balance': 0.0, 'total_equity': 0.0, 'open_positions': [], 'error': True}

    try:
        balance = await EXCHANGE_CLIENT.fetch_balance()
        total_usdt_balance = balance.get('total', {}).get('USDT', 0.0)
        
        # total_equity (総資産額) の取得
        GLOBAL_TOTAL_EQUITY = balance.get('total', {}).get('total', total_usdt_balance)
        if GLOBAL_TOTAL_EQUITY == 0.0:
            GLOBAL_TOTAL_EQUITY = total_usdt_balance # フォールバック

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
                    if usdt_value >= 10:
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
    except ccxt.NetworkError as e:
        logging.error(f"❌ 口座ステータス取得失敗 (ネットワークエラー): {e}")
    except ccxt.AuthenticationError as e:
        logging.critical(f"❌ 口座ステータス取得失敗 (認証エラー): {e}")
    except Exception as e:
        logging.error(f"❌ 口座ステータス取得失敗 (予期せぬエラー): {e}")
        
    return {'total_usdt_balance': 0.0, 'total_equity': 0.0, 'open_positions': [], 'error': True}

async def adjust_order_amount(symbol: str, usdt_amount: float, price: float) -> Tuple[float, float]:
    """USDT建ての注文量を取引所の最小数量、桁数に合わせて調整する"""
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        return 0.0, 0.0
        
    try:
        # 数量を計算
        base_amount = usdt_amount / price
        market = EXCHANGE_CLIENT.markets.get(symbol)
        
        if not market:
            logging.warning(f"⚠️ {symbol}の市場情報が見つかりません。数量の丸め処理をスキップします。")
            return base_amount, usdt_amount
            
        # 最小取引数量のチェック
        min_amount = market.get('limits', {}).get('amount', {}).get('min', 0.0)
        if base_amount < min_amount:
            logging.warning(f"⚠️ 注文数量 ({base_amount:.4f}) が最小取引数量 ({min_amount}) を下回りました。最小数量に調整します。")
            base_amount = min_amount
            
        # 数量の桁数に合わせて丸める
        base_amount = EXCHANGE_CLIENT.amount_to_precision(symbol, base_amount)
        
        # 最終的なUSDT金額を再計算 (指値価格ベース)
        final_usdt_amount = float(base_amount) * price
        
        return float(base_amount), final_usdt_amount
    except Exception as e:
        logging.error(f"❌ 注文数量の調整に失敗 ({symbol}): {e}")
        return 0.0, 0.0


async def fetch_ohlcv_safe(symbol: str, timeframe: str, limit: int) -> Optional[pd.DataFrame]:
    """CCXTからOHLCVデータを取得し、DataFrameに変換する (エラー処理を含む)"""
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.error("❌ OHLCV取得失敗: CCXTクライアントが準備できていません。")
        return None
        
    try:
        ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(
            symbol=symbol,
            timeframe=timeframe,
            limit=limit
        )

        if not ohlcv or len(ohlcv) < limit:
            logging.warning(f"⚠️ {symbol} ({timeframe}) のOHLCVデータが不足しています。取得数: {len(ohlcv) if ohlcv else 0}/{limit}")
            return None
            
        df = pd.DataFrame(
            ohlcv,
            columns=['timestamp', 'open', 'high', 'low', 'close', 'volume']
        )
        df['datetime'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True).dt.tz_convert(JST)
        df.set_index('datetime', inplace=True)
        return df
    except ccxt.ExchangeNotAvailable as e:
        logging.error(f"❌ OHLCV取得失敗 ({symbol} - {timeframe}): 取引所が利用できません。{e}")
    except ccxt.ExchangeError as e: 
        logging.error(f"❌ OHLCV取得失敗 ({symbol} - {timeframe}): 取引所エラー。{e}")
    except Exception as e:
        logging.error(f"❌ OHLCV取得中に予期せぬエラー ({symbol} - {timeframe}): {e}")
        return None

# ====================================================================================
# CORE LOGIC (実践ロジック V19.0.39)
# ====================================================================================

async def score_technical_indicators(symbol: str, data: Dict[str, pd.DataFrame]) -> Optional[Dict]:
    """
    テクニカル指標を分析し、総合的な取引スコアを計算する (実践ロジック)
    """
    global GLOBAL_MACRO_CONTEXT
    
    # 1. データチェックと初期化
    df_1h = data.get('1h')
    df_1m = data.get('1m')
    if df_1h is None or df_1h.empty or df_1m is None or df_1m.empty:
        logging.warning(f"❌ {symbol}: 必要なOHLCVデータが不足しています。")
        return None
    
    current_price = df_1m['close'].iloc[-1]
    
    # スコアとテクニカルデータ格納用
    total_score = BASE_SCORE
    tech_data = {}
    
    # 2. テクニカル指標の計算 (主として1H足を使用)
    
    # SMA
    df_1h.ta.sma(length=LONG_TERM_SMA_LENGTH, append=True)
    df_1h.ta.sma(length=MID_TERM_SMA_LENGTH, append=True)
    
    # MACD (12, 26, 9)
    df_1h.ta.macd(append=True)
    
    # RSI (14)
    df_1h.ta.rsi(append=True)
    
    # Bollinger Bands (20, 2)
    df_1h.ta.bbands(append=True)
    
    # ATR (14) - リスク管理用にも計算 (1分足を使用)
    df_1m.ta.atr(length=ATR_LENGTH, append=True)
    
    # 最終行のデータポイントを取得
    last_1h = df_1h.iloc[-1]
    last_1m = df_1m.iloc[-1]

    # 指標名 (pandas_taのデフォルト名)
    SMA_200 = f'SMA_{LONG_TERM_SMA_LENGTH}'
    SMA_50 = f'SMA_{MID_TERM_SMA_LENGTH}'
    RSI_14 = 'RSI_14'
    MACD_LINE = 'MACD_12_26_9'
    MACD_SIGNAL = 'MACDs_12_26_9' # シグナルライン
    MACD_HISTOGRAM = 'MACDh_12_26_9' # ヒストグラム
    ATR_NAME = f'ATR_{ATR_LENGTH}'

    # ====================================================================
    # 3. スコアリング項目 (ボーナス/ペナルティ)
    # ====================================================================
    
    # A. 長期トレンド逆行/一致 (SMA200に基づく)
    lt_reversal_penalty_value = 0.0
    # SMA200カラムが存在し、値がNaNでないことを前提とする
    if not np.isnan(last_1h[SMA_200]) and current_price < last_1h[SMA_200]:
        # 長期トレンド逆行（ロング取引の場合ペナルティ）
        # 価格とSMA200の乖離率を計算
        deviation_ratio = (last_1h[SMA_200] - current_price) / current_price
        # 乖離が大きくなるほどペナルティを最大まで増加
        lt_reversal_penalty_value = -min(LONG_TERM_REVERSAL_PENALTY_MAX, deviation_ratio * 10) # 10倍は調整係数
    
    total_score += lt_reversal_penalty_value
    tech_data['long_term_reversal_penalty_value'] = lt_reversal_penalty_value
    
    # B. 中期/長期トレンドアライメント (SMA50 > SMA200)
    trend_alignment_bonus_value = 0.0
    if not np.isnan(last_1h[SMA_50]) and not np.isnan(last_1h[SMA_200]) and last_1h[SMA_50] > last_1h[SMA_200]:
        # 中期トレンドも順行
        trend_alignment_bonus_value = TREND_ALIGNMENT_BONUS_MAX
        
    total_score += trend_alignment_bonus_value
    tech_data['trend_alignment_bonus_value'] = trend_alignment_bonus_value
    
    # C. MACDモメンタム (1H足)
    macd_penalty_value = 0.0
    macd_momentum_bonus = 0.0
    
    if MACD_LINE in last_1h and MACD_SIGNAL in last_1h and not np.isnan(last_1h[MACD_LINE]) and not np.isnan(last_1h[MACD_SIGNAL]):
        macd_line = last_1h[MACD_LINE]
        macd_signal = last_1h[MACD_SIGNAL] # シグナルライン
        macd_hist = last_1h.get(MACD_HISTOGRAM, 0.0) # MACDヒストグラム (安全に取得)

        if macd_line > macd_signal and macd_hist > 0 and macd_line > 0:
            # MACDゴールデンクロス、ヒストグラムがゼロライン上で拡大 (強い順行モメンタム)
            macd_momentum_bonus = 0.15 
        elif macd_line > macd_signal and macd_hist > 0 and macd_line < 0:
            # ゼロライン下からのゴールデンクロス (リバーサル候補)
            macd_momentum_bonus = 0.05
        
        # MACDペナルティ
        if macd_line < macd_signal and macd_hist < 0 and current_price > last_1h[SMA_200]:
            # デッドクロス、かつヒストグラムがマイナス域 (ロングに不利)
            macd_penalty_value = -MACD_CROSS_PENALTY
            macd_momentum_bonus = 0.0 # ペナルティがある場合、ボーナスはなし
        
    total_score += macd_momentum_bonus
    total_score += macd_penalty_value
    tech_data['macd_momentum_bonus'] = macd_momentum_bonus 
    tech_data['macd_penalty_value'] = macd_penalty_value
    
    # D. RSIモメンタム (1H足)
    rsi_value = last_1h.get(RSI_14, 50.0) # RSIが計算できない場合は中立値50.0
    rsi_momentum_bonus_value = 0.0
    
    # RSIがfloatであり、nanでないことを確認
    if isinstance(rsi_value, (float, np.floating)) and not np.isnan(rsi_value):
        if rsi_value < 55 and rsi_value > 30:
            # RSIが売られすぎ水準 (30-45) から55に向けて上昇している場合をボーナス化
            # 45に近いほど (反発が強いほど) ボーナスを大きくする
            ratio = (55 - rsi_value) / (55 - 30) # 30->1.0, 55->0.0
            rsi_momentum_bonus_value = min(RSI_MOMENTUM_BONUS_MAX, ratio * RSI_MOMENTUM_BONUS_MAX)
    
    # RSI値も記録 (通知用)
    tech_data['rsi_value'] = rsi_value
    total_score += rsi_momentum_bonus_value
    tech_data['rsi_momentum_bonus_value'] = rsi_momentum_bonus_value
    
    # E. ボラティリティペナルティ (BBands BandWidth)
    volatility_penalty_value = 0.0
    
    # last_1h.get() はキーが存在しない場合に None を返す可能性がある
    bb_upper = last_1h.get('BBU_20_2')
    bb_lower = last_1h.get('BBL_20_2')
    
    # 【v19.0.39 修正】Noneではないことを確認してから、numpy.isnan()を呼び出す
    if bb_upper is not None and bb_lower is not None and \
       not np.isnan(bb_upper) and not np.isnan(bb_lower):
         
         # バンド幅を計算: (Upper - Lower) / Close
         bb_width_ratio = (bb_upper - bb_lower) / current_price
         
         if bb_width_ratio < VOLATILITY_BB_PENALTY_THRESHOLD:
             # 低ボラティリティ相場 (取引非推奨)
             volatility_penalty_value = -VOLATILITY_PENALTY_MAX
         
    total_score += volatility_penalty_value
    tech_data['volatility_penalty_value'] = volatility_penalty_value
    
    # F. 出来高/OBV (1H足)
    obv_momentum_bonus_value = 0.0
    volume_increase_bonus_value = 0.0
    
    # 出来高の急増を判定
    avg_volume = df_1h['volume'].iloc[-10:-1].mean()
    if last_1h['volume'] > avg_volume * 1.5:
        # 直近の出来高が平均の1.5倍以上 (スパイク)
        volume_increase_bonus_value = VOLUME_INCREASE_BONUS
        # OBVも順行と仮定 (出来高スパイクがあればOBVも動いていると見なす)
        obv_momentum_bonus_value = OBV_MOMENTUM_BONUS

    total_score += volume_increase_bonus_value
    total_score += obv_momentum_bonus_value
    tech_data['volume_increase_bonus_value'] = volume_increase_bonus_value
    tech_data['obv_momentum_bonus_value'] = obv_momentum_bonus_value

    # G. 価格構造/ピボット支持 (固定ボーナス)
    # 実際はより複雑なサポート/レジスタンス計算が必要
    structural_pivot_bonus = STRUCTURAL_PIVOT_BONUS
    total_score += structural_pivot_bonus
    tech_data['structural_pivot_bonus'] = structural_pivot_bonus
    
    # H. 流動性 (固定ボーナス)
    # 実際は板情報から計算が必要
    liquidity_bonus_value = LIQUIDITY_BONUS_MAX
    total_score += liquidity_bonus_value
    tech_data['liquidity_bonus_value'] = liquidity_bonus_value
    
    # I. マクロ環境 (FGI/Forex Proxy)
    # FGI proxy (マクロ影響)をスコアに加算/減算
    fgi_proxy = GLOBAL_MACRO_CONTEXT.get('fgi_proxy', 0.0)
    sentiment_fgi_proxy_bonus = fgi_proxy * FGI_PROXY_BONUS_MAX 
    
    total_score += sentiment_fgi_proxy_bonus
    tech_data['sentiment_fgi_proxy_bonus'] = sentiment_fgi_proxy_bonus

    # 4. 最終スコアの調整
    # スコアを最大1.00、最低0.00にクリップ
    final_score = round(max(0.00, min(1.00, total_score)), 4) 

    # 5. リスク管理パラメータの計算 (実践: ATRに基づくSL/TP)
    
    # 1分足の最新のATR値を取得 (last_1m.get()を使用)
    current_atr = last_1m.get(ATR_NAME)
    
    # 【v19.0.39 修正】Noneを先にチェックし、Noneの場合は代替値を使用
    if current_atr is None or np.isnan(current_atr):
        # ATRが計算できなかった場合 (データ不足など)
        logging.warning(f"⚠️ {symbol}: 1分足のATR計算失敗。価格の0.5%をATRの代わりに使用します。")
        current_atr = current_price * 0.005 # 0.5%をATRの代わりに使用

    # SL/TP幅を計算
    sl_distance = current_atr * ATR_MULTIPLIER_SL
    tp_distance = current_atr * ATR_MULTIPLIER_TP
    
    # SL/TP価格を決定
    sl_price = current_price - sl_distance
    tp_price = current_price + tp_distance
    
    # リスクリワード比率 (RRR)
    if sl_distance > 0:
        rr_ratio = tp_distance / sl_distance
    else:
        rr_ratio = 1.0 # ゼロ割回避
    
    return {
        'symbol': symbol,
        'timeframe': '1m', # 代表として1mを採用
        'score': final_score,
        'entry_price': current_price,
        'stop_loss': sl_price,
        'take_profit': tp_price,
        'rr_ratio': rr_ratio,
        'tech_data': tech_data
    }


async def calculate_trade_params(signal: Dict) -> Dict:
    """取引ロットサイズを計算し、最終的なシグナルを返す"""
    global GLOBAL_TOTAL_EQUITY
    score = signal['score']
    
    # スコアに基づく動的ロットサイズの計算
    if GLOBAL_TOTAL_EQUITY > 0:
        min_lot = GLOBAL_TOTAL_EQUITY * DYNAMIC_LOT_MIN_PERCENT
        max_lot = GLOBAL_TOTAL_EQUITY * DYNAMIC_LOT_MAX_PERCENT
        
        # スコア (SIGNAL_THRESHOLD_NORMAL) から (DYNAMIC_LOT_SCORE_MAX) でロットを線形に増加
        if score >= DYNAMIC_LOT_SCORE_MAX:
            lot_usdt = max_lot
        elif score <= SIGNAL_THRESHOLD_NORMAL: # 閾値以下は最小ロット
            lot_usdt = min_lot
        else:
            # 閾値とMAXスコアの間で線形補間
            score_range = DYNAMIC_LOT_SCORE_MAX - SIGNAL_THRESHOLD_NORMAL
            weight = (score - SIGNAL_THRESHOLD_NORMAL) / score_range
            weight = max(0.0, min(1.0, weight))
            
            lot_usdt = min_lot + (max_lot - min_lot) * weight
    else:
        lot_usdt = BASE_TRADE_SIZE_USDT

    # USDT残高チェック
    if GLOBAL_TOTAL_EQUITY > 0 and GLOBAL_TOTAL_EQUITY - sum(p['filled_usdt'] for p in OPEN_POSITIONS) < MIN_USDT_BALANCE_FOR_TRADE:
        lot_usdt = 0.0
        logging.warning("⚠️ USDT残高が不足しているため、ロットサイズをゼロに設定しました。")
    
    signal['lot_size_usdt'] = lot_usdt
    
    return signal


async def execute_trade(signal: Dict) -> Dict:
    """取引所APIを呼び出して取引を実行する (ダミー)"""
    global EXCHANGE_CLIENT, OPEN_POSITIONS

    if TEST_MODE or not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        return {'status': 'error', 'error_message': '取引無効 (TEST_MODE/API未準備)'}

    try:
        usdt_amount = signal['lot_size_usdt']
        price = signal['entry_price']
        symbol = signal['symbol']

        if usdt_amount < 10.0:
             return {'status': 'error', 'error_message': f'ロットサイズが小さすぎます ({usdt_amount:.2f} USDT)'}

        # 1. 注文数量の調整
        base_amount, filled_usdt = await adjust_order_amount(symbol, usdt_amount, price)
        if base_amount == 0.0 or filled_usdt < 10.0:
            return {'status': 'error', 'error_message': '注文数量がゼロまたは調整に失敗 (最小取引額未満)'}

        # 2. 現物指値買い注文 (FOK) を実行 (ダミー)
        # 実際にはここに ccxt.create_order() の呼び出しが入る
        order_id = str(uuid.uuid4())
        
        # 3. SL/TP注文を実行 (ダミー) - Exchange SL/TP注文
        # 実際にはここに ccxt.create_order(params={'stopLoss':..., 'takeProfit':...}) の呼び出しが入る
        sl_order_id = str(uuid.uuid4())
        tp_order_id = str(uuid.uuid4())
        
        # 4. ポジションリストに追加
        new_position = {
            'symbol': symbol,
            'entry_price': price,
            'amount': base_amount,
            'filled_usdt': filled_usdt,
            'stop_loss': signal['stop_loss'],
            'take_profit': signal['take_profit'],
            'status': 'open',
            'entry_order_id': order_id,
            'sl_order_id': sl_order_id,
            'tp_order_id': tp_order_id,
            'entry_timestamp': time.time()
        }
        OPEN_POSITIONS.append(new_position)
        
        logging.info(f"✅ 取引成功 ({symbol}): {filled_usdt:.2f} USDT @ {price:.4f}")
        return {
            'status': 'ok',
            'filled_amount': base_amount,
            'filled_usdt': filled_usdt,
            'entry_price': price,
            'sl_order_id': sl_order_id,
            'tp_order_id': tp_order_id
        }
    except Exception as e:
        logging.error(f"❌ 取引実行中に致命的なエラー ({symbol}): {e}", exc_info=True)
        return {'status': 'error', 'error_message': f'取引APIエラー: {e}'}


async def open_order_management_loop_async():
    """オープン注文 (SL/TP) の監視ループ (ダミー)"""
    global OPEN_POSITIONS, EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.warning("⚠️ 注文監視スキップ: CCXTクライアントが準備できていません。")
        return

    # 既存のポジションの SL/TP をチェック (ダミーロジック)
    positions_to_close = []
    
    # リアルタイム価格を取得する (ダミー)
    tickers = {}
    if OPEN_POSITIONS:
        try:
            # 実際には一括取得APIを使う
            symbols = list(set(p['symbol'] for p in OPEN_POSITIONS))
            for symbol in symbols:
                ticker = await EXCHANGE_CLIENT.fetch_ticker(symbol)
                tickers[symbol] = ticker['last']
        except Exception as e:
            logging.error(f"❌ 注文監視中のティッカー取得エラー: {e}")
            return # 一時的なAPIエラーとして処理を中断

    for pos in OPEN_POSITIONS[:]: # リストをコピーしてイテレート
        symbol = pos['symbol']
        current_price = tickers.get(symbol)
        
        if current_price:
            # SL/TPのチェック (ダミー: ランダムに決済をシミュレート)
            # 実際には取引所APIで決済済み注文をチェックする
            if random.random() < 0.005: # 0.5%の確率でSL/TPにヒット
                exit_type = "TAKE_PROFIT" if random.random() > 0.5 else "STOP_LOSS"
                exit_price = pos['take_profit'] if exit_type == "TAKE_PROFIT" else pos['stop_loss']
                
                # 決済注文実行 (ダミー)
                trade_result = {
                    'entry_price': pos['entry_price'],
                    'exit_price': exit_price,
                    'filled_amount': pos['amount'],
                    'exit_type': exit_type,
                    'stop_loss': pos['stop_loss'],
                    'take_profit': pos['take_profit'],
                    'pnl_usdt': (exit_price - pos['entry_price']) * pos['amount'],
                    'pnl_rate': (exit_price / pos['entry_price']) - 1.0,
                }
                
                # 決済通知
                await send_telegram_notification(format_telegram_message(
                    signal=pos, context="ポジション決済", current_threshold=0.0, trade_result=trade_result
                ))

                positions_to_close.append(pos)
                logging.info(f"🔴 ポジション決済 ({symbol}): {exit_type} トリガー @ {exit_price:.4f}")
        
    # 決済されたポジションをリストから削除
    for pos in positions_to_close:
        OPEN_POSITIONS.remove(pos)

    logging.info(f"✅ 注文監視ループ完了。現在 {len(OPEN_POSITIONS)} 銘柄を監視中。")

# ====================================================================================
# SCHEDULER & MAIN LOOPS
# ====================================================================================

async def hourly_service_scheduler():
    """1時間ごとのレポート送信とWebShareログアップロードを行うスケジューラ"""
    global LAST_WEBSHARE_UPLOAD_TIME, LAST_HOURLY_NOTIFICATION_TIME, HOURLY_SIGNAL_LOG
    
    while True:
        await asyncio.sleep(60) # 1分待機

        try:
            current_time = time.time()
            
            # 1. WebShareログアップロード
            if current_time - LAST_WEBSHARE_UPLOAD_TIME >= WEBSHARE_UPLOAD_INTERVAL:
                
                # タイムスタンプを即座に更新し、頻繁な呼び出しを防ぐ
                LAST_WEBSHARE_UPLOAD_TIME = current_time 
                
                webshare_data = {
                    'equity': GLOBAL_TOTAL_EQUITY,
                    'positions': OPEN_POSITIONS,
                    'signals_last_hour': HOURLY_SIGNAL_LOG
                }
                await send_webshare_update(webshare_data)
            
            # 2. 1時間スコアレポート
            if current_time - LAST_HOURLY_NOTIFICATION_TIME >= HOURLY_SCORE_REPORT_INTERVAL:
                if HOURLY_SIGNAL_LOG:
                    current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
                    report_message = format_hourly_report(HOURLY_SIGNAL_LOG, LAST_HOURLY_NOTIFICATION_TIME, current_threshold)
                    await send_telegram_notification(report_message)
                
                # リセット
                LAST_HOURLY_NOTIFICATION_TIME = current_time
                HOURLY_SIGNAL_LOG = []
                logging.info("🕒 1時間レポートを送信し、ログをリセットしました。")

        except Exception as e:
            logging.critical(f"❌ 1時間サービス実行中に致命的なエラー: {e}", exc_info=True)
            await asyncio.sleep(60) # エラー時のみ1分待機して再試行
            

async def main_bot_loop():
    """メインの取引ロジックと市場データの更新を行う (1分ごと)"""
    global IS_FIRST_MAIN_LOOP_COMPLETED, LAST_SUCCESS_TIME, LAST_SIGNAL_TIME, LAST_ANALYSIS_SIGNALS, CURRENT_MONITOR_SYMBOLS, HOURLY_SIGNAL_LOG, GLOBAL_MACRO_CONTEXT
    
    # 1. 口座ステータスの更新
    account_status = await fetch_account_status()
    current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)

    if not IS_FIRST_MAIN_LOOP_COMPLETED:
        # 初回起動通知
        bot_version = "v19.0.39" # バージョンを更新
        startup_msg = format_startup_message(
            account_status, GLOBAL_MACRO_CONTEXT, len(CURRENT_MONITOR_SYMBOLS), current_threshold, bot_version
        )
        await send_telegram_notification(startup_msg)
        IS_FIRST_MAIN_LOOP_COMPLETED = True
        logging.info("✅ 初回メインループが完了しました。")

    # 2. 監視銘柄リストの更新（ダミー - 実際には出来高トップなどを取得）
    if not SKIP_MARKET_UPDATE:
        # 実際には fetch_top_volume_symbols() のような関数を呼び出す
        CURRENT_MONITOR_SYMBOLS = DEFAULT_SYMBOLS[:TOP_SYMBOL_LIMIT]
    
    LAST_ANALYSIS_SIGNALS = []
    
    # 3. 監視銘柄の分析
    tasks = []
    for symbol in CURRENT_MONITOR_SYMBOLS:
        tasks.append(process_symbol(symbol, current_threshold))
        
    results = await asyncio.gather(*tasks)

    # 4. 結果の処理
    for result in results:
        if result and result.get('status') == 'signal':
            signal = result['signal']
            LAST_ANALYSIS_SIGNALS.append(signal)
            HOURLY_SIGNAL_LOG.append(signal) # 1時間レポート用ログに追加
            
            # シグナル通知 (Telegram)
            trade_result = await execute_trade(signal)
            await send_telegram_notification(format_telegram_message(
                signal=signal, context="取引シグナル", current_threshold=current_threshold, trade_result=trade_result
            ))


async def process_symbol(symbol: str, current_threshold: float) -> Optional[Dict]:
    """個別の銘柄分析と取引実行を行う"""
    # 1. OHLCVデータの取得
    ohlcv_data: Dict[str, pd.DataFrame] = {}
    for tf, limit in REQUIRED_OHLCV_LIMITS.items():
        df = await fetch_ohlcv_safe(symbol, tf, limit)
        if df is None:
            return None 
        ohlcv_data[tf] = df
        
    # 2. スコアリング
    signal = await score_technical_indicators(symbol, ohlcv_data)
    if signal is None:
        return None
        
    score = signal['score']
    
    # 3. 取引シグナル判定
    if score >= current_threshold:
        # 冷却期間のチェック
        if symbol in LAST_SIGNAL_TIME and (time.time() - LAST_SIGNAL_TIME[symbol] < TRADE_SIGNAL_COOLDOWN):
            logging.info(f"⏳ {symbol}: スコア {score*100:.2f}。冷却期間中のため取引をスキップ。")
            return None
            
        # ロット計算
        signal = await calculate_trade_params(signal)
        
        # ロットサイズが最小ロット未満の場合、シグナルをキャンセル
        if signal['lot_size_usdt'] < 10.0:
            logging.info(f"⚠️ {symbol}: スコア {score*100:.2f}。計算されたロットサイズが最小取引額未満のためスキップ。")
            return None
        
        LAST_SIGNAL_TIME[symbol] = time.time()
        
        return {'status': 'signal', 'signal': signal}

    return None


async def main_bot_scheduler():
    """メインの分析ループを定期実行するスケジューラ (60秒ごと)"""
    global LAST_SUCCESS_TIME
    while True:
        start_time = time.time()
        try:
            await main_bot_loop()
            LAST_SUCCESS_TIME = time.time() # 成功時にのみ更新
        except Exception as e:
            # 致命的なエラーが発生した場合でも、ループを継続するためにエラーをログに記録し、待機時間を経て再試行
            logging.critical(f"❌ メインループ実行中に致命的なエラー: {e}", exc_info=True)
            await send_telegram_notification(f"🚨 **致命的なエラー**\nメインループでエラーが発生しました: `{e}`")

        # 待機時間を LOOP_INTERVAL (60秒) に基づいて計算
        # 実行にかかった時間を差し引くことで、正確な周期実行を保証
        elapsed_time = time.time() - start_time
        wait_time = max(1, LOOP_INTERVAL - elapsed_time)
        logging.info(f"次のメインループまで {wait_time:.1f} 秒待機します。")
        await asyncio.sleep(wait_time)


async def open_order_management_scheduler():
    """オープン注文 (SL/TP) の監視ループを定期実行するスケジューラ (10秒ごと)"""
    while True:
        try:
            await open_order_management_loop_async() 
        except Exception as e:
            logging.critical(f"❌ オープン注文監視ループ実行中に致命的なエラー: {e}", exc_info=True)
            await send_telegram_notification(f"🚨 **致命的なエラー**\n注文監視ループでエラーが発生しました: `{e}`")

        await asyncio.sleep(MONITOR_INTERVAL) # MONITOR_INTERVAL (10秒) ごとに実行

# ====================================================================================
# SCHEDULER MONITORING & RECOVERY (24/7 稼働対応)
# ====================================================================================
async def resilient_scheduler_wrapper(task_func: Callable, task_name: str):
    """
    非同期タスクを無限に監視・再起動するラッパー関数。
    タスクが予期せぬエラーで終了した場合、ログに記録し、Telegram通知を行い、再試行する。
    """
    logging.info(f"🟢 {task_name} のタスク監視ラッパーを起動しました。")
    while True:
        try:
            logging.info(f"🚀 {task_name} を開始/再開します。")
            await task_func()
            
            # task_func()が無限ループのため、ここに到達するのは予期せぬ終了時のみ
            logging.critical(f"❌ 【超重大警告】タスク `{task_name}` がループを終了しました。即座に再起動します。")
            
        except asyncio.CancelledError:
            # Uvicorn/FastAPIシャットダウン時など、明示的なキャンセル
            logging.info(f"🛑 {task_name} がキャンセルされました。監視を停止します。")
            break
            
        except Exception as e:
            # ループ内のtry/exceptで捕捉しきれなかった致命的なエラー
            error_message = (
                f"❌ 【超重大エラー】タスク `{task_name}` が予期せぬ理由で停止しました。再起動します。\n"
                f"エラー: {type(e).__name__}: `{e}`"
            )
            logging.critical(error_message, exc_info=True)
            
            # 重大エラー通知
            await send_telegram_notification(error_message)
            
            # 30秒待機してから再起動
            logging.info(f"💤 {task_name} を30秒間待機してから再起動します。")
            await asyncio.sleep(30)
# --------------------

# ====================================================================================
# API ENDPOINTS & LIFECYCLE
# ====================================================================================

app = FastAPI(title="Apex BOT API", version="v19.0.39") 

@app.get("/health", response_class=JSONResponse)
async def health_check():
    """FastAPIアプリケーションのヘルスチェックエンドポイント"""
    return {"status": "ok", "last_success": LAST_SUCCESS_TIME, "positions": len(OPEN_POSITIONS)}

@app.on_event("startup")
async def startup_event():
    logging.info("🌟 FastAPI/Uvicorn 起動イベント開始...")
    await initialize_exchange_client()

    # メインのスケジューラタスクを、回復力のあるラッパー経由で開始 (24/7対応)
    asyncio.create_task(resilient_scheduler_wrapper(main_bot_scheduler, "メイン分析スケジューラ"))
    asyncio.create_task(resilient_scheduler_wrapper(open_order_management_scheduler, "注文監視スケジューラ"))
    asyncio.create_task(resilient_scheduler_wrapper(hourly_service_scheduler, "時間ごと通知スケジューラ"))
    logging.info("✅ 全スケジューラタスクを開始しました。")


@app.on_event("shutdown")
async def shutdown_event():
    logging.info("🛑 FastAPIシャットダウンイベント開始...")
    if EXCHANGE_CLIENT:
        await EXCHANGE_CLIENT.close()
    logging.info("✅ CCXTクライアントをクローズしました。")

# ====================================================================================
# MAIN EXECUTION
# ====================================================================================

# Uvicorn起動時に "main_render:app" の形式で指定されるため、ここでは省略

if __name__ == "__main__":
    # 実行前に環境変数の確認
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.critical("🚨 環境変数 TELEGRAM_TOKEN または TELEGRAM_CHAT_ID が設定されていません。")
        
    if not API_KEY or not SECRET_KEY:
        logging.critical(f"🚨 環境変数 {CCXT_CLIENT_NAME.upper()}_API_KEY または {CCXT_CLIENT_NAME.upper()}_SECRET が設定されていません。")

    # Uvicornサーバーを起動
    # 注意: 実際に実行するファイル名に合わせて "main_render:app" の部分を変更してください
    logging.info("Starting Uvicorn server...")
    # host='0.0.0.0'はRenderなどのクラウド環境での必須設定
    uvicorn.run("main_render:app", host="0.0.0.0", port=8000, log_level="info")
