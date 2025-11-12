# ====================================================================================
# Apex BOT v19.0.47 - CRITICAL FIX: IOC Trade Misdetection & SL/TP Failure Handling
#
# 改良・修正点:
# 1. 【最重要修正】execute_trade関数内のIOC注文結果の確認ロジックを強化。
#    - IOC注文が部分約定または全約定した場合に、filled_amount > 0.0 のチェックを最優先し、
#      確実にSL/TP設定プロセスへ進むようにロジックを修正しました。
# 2. 【SL/TP失敗時】SL/TP設定に失敗した場合、その後の強制クローズの結果を取引結果に含め、
#    エラー通知がより正確な情報（例: SL/TP設定失敗、ポジション強制クローズ成功）を伝えるようにしました。
# 3. BOT_VERSION を v19.0.47 に更新。
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

# ★ 新規追加: ボットのバージョン (v19.0.47 修正点)
BOT_VERSION = "v19.0.47"

if TEST_MODE:
    logging.warning("⚠️ WARNING: TEST_MODE is active. Trading is disabled.")

# CCXTクライアントの準備完了フラグ
IS_CLIENT_READY: bool = False

# 取引ルール設定
TRADE_SIGNAL_COOLDOWN = 60 * 60 * 2 # 同一銘柄のシグナル通知クールダウン（2時間）
SIGNAL_THRESHOLD = 0.65             # 動的閾値のベースライン
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

# 市場環境に応じた動的閾値調整のための定数 (変更なし)
FGI_SLUMP_THRESHOLD = -0.02         
FGI_ACTIVE_THRESHOLD = 0.02         
SIGNAL_THRESHOLD_SLUMP = 0.85       
SIGNAL_THRESHOLD_NORMAL = 0.83      
SIGNAL_THRESHOLD_ACTIVE = 0.80      

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
            # ユーザー要望による追加: 決済セクションに指値価格を追加
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
        # ★ここから価格表示をformat_price_precisionに変更
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
    
    # 💡 失敗セクションがあれば追加
    if failure_section:
        message += failure_section + f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        
    # 💡 スコア詳細ブレークダウンは、シグナル通知のコンテキストでのみ、成功/失敗に関わらず追加する
    if context == "取引シグナル":
        message += (
            f"  \n**📊 スコア詳細ブレークダウン** (+/-要因)\n"
            f"{breakdown_details}\n"
            f"  <code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        )
        
    # ★ v19.0.47 修正点: BOT_VERSION を使用
    message += (f"<i>Bot Ver: {BOT_VERSION} - Full Analysis & Async Refactoring</i>")
    return message

def format_hourly_report(signals: List[Dict], attempt_log: Dict[str, str], start_time: float, current_threshold: float, bot_version: str) -> str:
    """
    1時間ごとの最高・最低スコア銘柄の通知メッセージを作成する。
    """
    
    now_jst = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")
    start_jst = datetime.fromtimestamp(start_time, JST).strftime("%H:%M:%S")
    
    # スコアでソート
    signals_sorted = sorted(signals, key=lambda x: x['score'], reverse=True)
    
    analyzed_count = len(signals)
    attempt_count = len(attempt_log) # 分析試行されたが、クールダウンなどでスキップされた銘柄
    
    # 総監視銘柄数から、分析をスキップされた銘柄を計算
    total_monitoring_count = len(CURRENT_MONITOR_SYMBOLS)
    skipped_count = total_monitoring_count - analyzed_count - attempt_count

    # 基本情報
    message = (
        f"🕒 **Apex BOT 1時間スコアレポート**\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - **集計日時**: {start_jst} - {now_jst} (JST)\n"
        f"  - **総監視銘柄数**: <code>{total_monitoring_count}</code>\n"
        f"  - **分析成功銘柄数**: <code>{analyzed_count}</code>\n"
    )
    
    if not signals_sorted:
        # シグナルがなかった場合のレポート
        message += (
            f"  - **レポート**: 過去1時間以内に有効な分析データが取得できませんでした。\n"
            f"  - **失敗・スキップ理由**: <code>データ取得失敗、指標計算エラー、クールダウンなど。ログを確認してください。</code>\n"
            f"  - **取引閾値**: <code>{current_threshold*100:.2f}</code> 点\n"
            f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
            f"<i>Bot Ver: {bot_version} - Full Analysis & Async Refactoring</i>"
        )
        return message

    best_signal = signals_sorted[0]
    worst_signal = signals_sorted[-1]
    
    # 閾値超え銘柄のカウント
    threshold_count = sum(1 for s in signals if s['score'] >= current_threshold)
    
    # 閾値情報を追加
    message += f"  - **取引閾値**: <code>{current_threshold*100:.2f}</code> 点\n"
    message += f"  - **閾値超え銘柄**: <code>{threshold_count}</code> 銘柄\n"
    message += f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
    
    # 🟢 ベストスコア銘柄
    message += (
        f"\n"
        f"🟢 **ベストスコア銘柄 (Top)**\n"
        f"  - **銘柄**: <b>{best_signal['symbol']}</b> ({best_signal['timeframe']})\n"
        f"  - **スコア**: <code>{best_signal['score'] * 100:.2f} / 100</code>\n"
        f"  - **推定勝率**: <code>{get_estimated_win_rate(best_signal['score'])}</code>\n"
        f"  - **指値 (Entry)**: <code>{format_price_precision(best_signal['entry_price'])}</code>\n"
        f"  - **SL/TP**: <code>{format_price_precision(best_signal['stop_loss'])}</code> / <code>{format_price_precision(best_signal['take_profit'])}</code>\n"
    )
    
    # 🔴 ワーストスコア銘柄
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
        'signal': _to_json_compatible(signal),
        'total_equity': GLOBAL_TOTAL_EQUITY,
        'current_positions_count': len(OPEN_POSITIONS),
    }
    
    # 実際にはここにファイルへの追記ロジックやデータベースへの書き込みロジックが入る
    return log_data


# ====================================================================================
# CCXT & DATA ACQUISITION
# ====================================================================================

async def send_telegram_notification(message: str) -> bool:
    """
    指定されたメッセージをTelegramに送信する非同期関数。
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.error("❌ Telegram設定が不足しています。通知をスキップします。")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    
    # URLに含めるパラメータ (HTMLパースモードを使用)
    params = {
        'chat_id': TELEGRAM_CHAT_ID,
        'text': message,
        'parse_mode': 'HTML' # HTMLタグ (<code>, <b>など) を使用するためHTMLモード
    }
    
    try:
        # requestsライブラリを使用 (ブロッキングの可能性があるため、本番環境では注意が必要)
        response = requests.post(url, data=params, timeout=10)
        response.raise_for_status()
        logging.info("✅ Telegram通知を送信しました。")
        return True
    except requests.exceptions.RequestException as e:
        logging.error(f"❌ Telegram通知の送信に失敗しました: {e}", exc_info=True)
        return False

async def initialize_exchange_client():
    """CCXTクライアントを初期化する"""
    global EXCHANGE_CLIENT, IS_CLIENT_READY
    
    if IS_CLIENT_READY:
        logging.info("ℹ️ CCXTクライアントはすでに初期化済みです。スキップします。")
        return

    ExchangeClass = None
    try:
        # CCXTクライアントを動的に取得
        if CCXT_CLIENT_NAME.lower() == 'mexc':
            ExchangeClass = ccxt_async.mexc
        elif CCXT_CLIENT_NAME.lower() == 'binance':
            ExchangeClass = ccxt_async.binance
        elif CCXT_CLIENT_NAME.lower() == 'bybit':
            ExchangeClass = ccxt_async.bybit
        else:
            logging.critical(f"❌ 未対応の取引所クライアント名: {CCXT_CLIENT_NAME}")
            return
            
        EXCHANGE_CLIENT = ExchangeClass({
            'apiKey': API_KEY,
            'secret': SECRET_KEY,
            'enableRateLimit': True, # レート制限を有効にする
            # CCXTの現物取引を有効にする設定 (取引所依存)
            'options': {
                'defaultType': 'spot',
                'recvWindow': 60000, # MEXCなどでタイムアウトを防ぐための設定
            },
        })
        
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
        logging.error("❌ 口座ステータス取得失敗: CCXTクライアントが未準備です。")
        return {'total_usdt_balance': 0.0, 'total_equity': 0.0, 'open_positions': [], 'error': True}

    try:
        # 残高の取得
        balance = await EXCHANGE_CLIENT.fetch_balance()
        
        # USDT残高の取得
        total_usdt_balance = balance.get('total', {}).get('USDT', 0.0)
        
        # total_equity (総資産額) の取得
        GLOBAL_TOTAL_EQUITY = balance.get('total', {}).get('total', total_usdt_balance)
        if GLOBAL_TOTAL_EQUITY == 0.0:
             GLOBAL_TOTAL_EQUITY = total_usdt_balance # フォールバック
             
        logging.info(f"✅ 口座ステータス取得成功: Equity={format_usdt(GLOBAL_TOTAL_EQUITY)} USDT, Free USDT={format_usdt(total_usdt_balance)}")
        
        # USDT以外の保有資産の評価
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
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.error("❌ 出来高TOP銘柄取得失敗: CCXTクライアントが未準備です。")
        return []

    try:
        # 出来高ベースでのランキング取得 (取引所APIによる)
        if EXCHANGE_CLIENT.has['fetchTickers']:
            tickers = await EXCHANGE_CLIENT.fetch_tickers()
            
            # USDTペアのみにフィルタリングし、出来高(quoteVolume)で降順ソート
            usdt_tickers = {
                s: t for s, t in tickers.items() 
                if '/USDT' in s and 
                   t.get('quoteVolume') is not None and 
                   t['quoteVolume'] > 100000 # 出来高が一定量以上
            }
            
            # quoteVolume(USDT建て出来高)でソートし、TOP 40を取得
            sorted_tickers = sorted(usdt_tickers.items(), key=lambda item: item[1]['quoteVolume'], reverse=True)
            top_symbols = [symbol for symbol, _ in sorted_tickers[:TOP_SYMBOL_LIMIT]]
            
            logging.info(f"✅ 出来高TOP {TOP_SYMBOL_LIMIT} 銘柄を取得しました。")
            return top_symbols

    except Exception as e:
        logging.error(f"❌ 出来高TOP銘柄の取得に失敗: {e}", exc_info=True)
    
    # 失敗した場合はデフォルトリストとBTC/ETHを返す
    return DEFAULT_SYMBOLS


async def fetch_fgi_data() -> Dict:
    """Fear & Greed Index (FGI) データを取得する"""
    
    FGI_API_URL = "https://api.alternative.me/fng/?limit=1"
    
    try:
        response = requests.get(FGI_API_URL, timeout=10)
        response.raise_for_status()
        data = response.json().get('data')
        
        if data:
            raw_value = int(data[0]['value']) # 0-100
            
            # Raw=0 (Extreme Fear) -> Proxy=-1.0, Raw=100 (Extreme Greed) -> Proxy=1.0
            # Raw=50 (Neutral) -> Proxy=0.0
            fgi_proxy = (raw_value - 50) / 50.0
            
            logging.info(f"✅ FGIデータ取得成功: Raw={raw_value}, Proxy={fgi_proxy:.2f}")
            
            return {
                'fgi_raw_value': raw_value,
                'fgi_proxy': fgi_proxy,
                'forex_bonus': 0.0, # 為替機能は削除
            }
        
        logging.warning("⚠️ FGIデータ取得失敗: APIデータが空です。")
    except requests.exceptions.RequestException as e:
        logging.error(f"❌ FGIデータ取得失敗 (ネットワークエラー): {e}")
        
    # 失敗時は中立を返す
    return {'fgi_proxy': 0.0, 'fgi_raw_value': 'N/A', 'forex_bonus': 0.0}

# ====================================================================================
# TRADING LOGIC
# ====================================================================================

def calculate_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """テクニカル指標を計算し、DataFrameに追加する"""
    
    # SMA
    df['SMA200'] = ta.sma(df['close'], length=LONG_TERM_SMA_LENGTH)
    df['SMA50'] = ta.sma(df['close'], length=50) # 中期トレンド用に追加
    
    # RSI
    df['RSI'] = ta.rsi(df['close'], length=14)
    
    # MACD
    macd_data = df.ta.macd(close='close', fast=12, slow=26, signal=9, append=False)
    # MACDの結果をDataFrameに追加
    df['MACD'] = macd_data['MACD_12_26_9']
    df['MACD_H'] = macd_data['MACDh_12_26_9']
    df['MACD_S'] = macd_data['MACDs_12_26_9']
    
    # Bollinger Bands
    bb_data = df.ta.bbands(close='close', length=20, std=2.0, append=False)
    # 💡 【BBANDSキーの修正】 Key 'BBL_20_2.0' not found エラーに対応
    df['BBL'] = bb_data['BBL_20_2.0_2.0']
    df['BBM'] = bb_data['BBM_20_2.0_2.0']
    df['BBU'] = bb_data['BBU_20_2.0_2.0']
    df['BBB'] = bb_data['BBB_20_2.0_2.0']
    
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
    if len(df) < 10 or df.isnull().values.any(): 
        # データが不十分または計算エラーでNaNが含まれる
        return None

    # 最新のローソク足データを取得
    last_candle = df.iloc[-1]
    last_close = last_candle['close']
    last_low = last_candle['low']
    
    # ATR (Average True Range) を使用したSL/TPの計算のために、まずATRを計算する
    atr_data = df.ta.atr(length=14, append=False)
    
    # ATRが計算できない場合 (例えばデータ不足) はシグナルを返さない
    if atr_data.empty or len(atr_data) < 1: 
        return None

    # 最新のATR値
    latest_atr = atr_data.iloc[-1]
    
    # 2. SL/TPの計算
    
    # Entry Price: 指値はローソク足の終値 (last_close)
    entry_price = last_close
    
    # SL: Entry Priceから2.5 ATR下の価格
    sl_multiplier = 2.5
    stop_loss = entry_price - (latest_atr * sl_multiplier)
    
    # TP: Entry Priceから5.0 ATR上の価格 (リスクリワード比率 RRR=1:2.0)
    tp_multiplier = 5.0 
    take_profit = entry_price + (latest_atr * tp_multiplier)
    
    # SL/TPが0以下になる場合は無効なシグナル
    if stop_loss <= 0.0: 
        return None

    # リスクリワード比率の計算
    risk = entry_price - stop_loss
    reward = take_profit - entry_price
    rr_ratio = reward / risk if risk > 0 else 0.0

    # 3. スコアリング
    
    # A. ベーススコア
    total_score = BASE_SCORE # 50点
    tech_data = {'base_score': BASE_SCORE, 'rsi_value': last_candle['RSI']}
    
    # B. 長期トレンド逆行ペナルティ
    # 乖離率が一定以上で、かつ価格がSMA200を大きく下回っている場合
    long_term_reversal_penalty_value = 0.0
    sma200 = last_candle['SMA200']
    price_deviation = (sma200 - last_close) / sma200
    
    # 価格がSMA200を大きく下回っている場合 (例: 5%以上)
    if price_deviation > 0.05:
        long_term_reversal_penalty_value = LONG_TERM_REVERSAL_PENALTY
        total_score -= long_term_reversal_penalty_value
    tech_data['long_term_reversal_penalty_value'] = long_term_reversal_penalty_value
    
    # C. トレンドアライメントボーナス (中期/長期トレンド一致)
    trend_alignment_bonus_value = 0.0
    # SMA50がSMA200を上回っていること
    if last_candle['SMA50'] > sma200:
        trend_alignment_bonus_value = TREND_ALIGNMENT_BONUS
        total_score += trend_alignment_bonus_value
    tech_data['trend_alignment_bonus_value'] = trend_alignment_bonus_value
    
    # D. 価格構造/ピボット支持ボーナス (簡易版: 過去の安値/高値からの離れ具合)
    structural_pivot_bonus = 0.0
    # 過去20本の最安値付近 (価格が過去20本の最安値から1%以内)
    low_20 = df['low'].iloc[-20:-1].min()
    if (last_close - low_20) / low_20 < 0.01:
        structural_pivot_bonus = STRUCTURAL_PIVOT_BONUS
        total_score += structural_pivot_bonus
    tech_data['structural_pivot_bonus'] = structural_pivot_bonus

    # E. MACDクロス/発散ペナルティ (MACD < Signal の場合ペナルティ)
    macd_penalty_value = 0.0
    macd = last_candle['MACD']
    macd_signal = last_candle['MACD_S']
    
    # MACDがシグナルを下回っている、つまりモメンタムが減速している場合
    if macd < macd_signal:
        macd_penalty_value = MACD_CROSS_PENALTY
        total_score -= macd_penalty_value
    tech_data['macd_penalty_value'] = macd_penalty_value

    # F. RSIモメンタムボーナス (RSIが50に向けて加速)
    rsi_momentum_bonus_value = 0.0
    rsi = last_candle['RSI']
    if RSI_MOMENTUM_LOW < rsi <= 70.0:
        # 50で0点、70でRSI_MOMENTUM_BONUS_MAX (0.10)
        # RSI 50から70の間で線形にボーナスを増加させる
        if rsi > 50.0:
            rsi_momentum_bonus_value = RSI_MOMENTUM_BONUS_MAX * ((rsi - 50.0) / 20.0)
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
    if 'Volume_SMA20' in df.columns and last_candle['Volume_SMA20'] > 0 and last_candle['volume'] > last_candle['Volume_SMA20'] * 1.5:
        # 出来高が平均の1.5倍
        volume_increase_bonus_value = VOLUME_INCREASE_BONUS
        total_score += volume_increase_bonus_value
    tech_data['volume_increase_bonus_value'] = volume_increase_bonus_value

    # I. Volatility Penalty (ボリンジャーバンド幅が狭すぎる場合)
    volatility_penalty_value = 0.0
    bb_width_percent = last_candle['BBB']
    if bb_width_percent < VOLATILITY_BB_PENALTY_THRESHOLD * 100: # BB幅が1%未満
        volatility_penalty_value = -0.05 # ペナルティとしてマイナス5点を付与
        total_score += volatility_penalty_value # マイナスの値を加算
    tech_data['volatility_penalty_value'] = volatility_penalty_value
    
    # J. 流動性ボーナス (板情報は省略しMAXボーナスを固定)
    liquidity_bonus_value = LIQUIDITY_BONUS_MAX
    total_score += liquidity_bonus_value
    tech_data['liquidity_bonus_value'] = liquidity_bonus_value
    
    # K. マクロ環境ボーナス/ペナルティ
    fgi_proxy = macro_context.get('fgi_proxy', 0.0)
    forex_bonus = macro_context.get('forex_bonus', 0.0)
    # FGIと為替の値を合計し、FGI_PROXY_BONUS_MAX (0.05)の範囲でスコアを増減させる
    sentiment_fgi_proxy_bonus = (fgi_proxy + forex_bonus) * FGI_PROXY_BONUS_MAX
    total_score += sentiment_fgi_proxy_bonus
    tech_data['sentiment_fgi_proxy_bonus'] = sentiment_fgi_proxy_bonus
    
    # 最終スコアを0.0から1.00の間にクランプ
    final_score = max(0.0, min(1.0, total_score))
    
    # 5. シグナルデータの構築
    symbol = market_ticker['symbol']
    
    signal_data = {
        'symbol': symbol,
        'timeframe': timeframe,
        'score': final_score,
        'entry_price': entry_price,
        'stop_loss': stop_loss,
        'take_profit': take_profit,
        'rr_ratio': rr_ratio,
        'tech_data': tech_data
    }
    
    return signal_data

def calculate_dynamic_lot_size(score: float, account_status: Dict) -> float:
    """総合スコアに基づき、総資産額に応じた動的ロットサイズ (USDT建て) を計算する"""
    global BASE_TRADE_SIZE_USDT
    
    total_equity = account_status.get('total_equity', 0.0)
    
    # 1. 最小ロットと最大ロットの計算
    min_lot = max(BASE_TRADE_SIZE_USDT, total_equity * DYNAMIC_LOT_MIN_PERCENT)
    max_lot = total_equity * DYNAMIC_LOT_MAX_PERCENT
    
    # 2. スコアに基づいた線形補間
    if score >= DYNAMIC_LOT_SCORE_MAX:
        final_lot = max_lot
    elif score <= SIGNAL_THRESHOLD:
        final_lot = min_lot
    else:
        # スコア範囲 (SIGNAL_THRESHOLD から DYNAMIC_LOT_SCORE_MAX) で線形に増加
        score_range = DYNAMIC_LOT_SCORE_MAX - SIGNAL_THRESHOLD
        lot_range = max_lot - min_lot
        
        if score_range > 0:
            final_lot = min_lot + lot_range * ((score - SIGNAL_THRESHOLD) / score_range)
        else:
            final_lot = min_lot

    # 💡 ロギング強化: 動的ロットサイズの詳細
    logging.info(
        f"💰 ロット計算: Score={score*100:.2f}. "
        f"Equity={format_usdt(total_equity)} USDT. "
        f"Min/Max Lot={format_usdt(min_lot)}/{format_usdt(max_lot)} USDT. "
        f"最終ロットサイズ: {format_usdt(final_lot)} USDT"
    )
    
    return final_lot

async def adjust_order_amount(symbol: str, usdt_amount: float, price: float) -> Tuple[float, float]:
    """
    取引所の最小数量、最小ロットサイズ、数量の精度に従って注文数量を調整する。
    Returns: (base_amount, final_usdt_amount)
    """
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        return 0.0, 0.0

    market = EXCHANGE_CLIENT.market(symbol)
    
    # 1. Base amount の計算 (購入数量)
    base_amount_unrounded = usdt_amount / price

    # 2. 数量の精度 (amount_precision) と最小数量 (min_amount) の取得
    amount_precision = market['precision']['amount'] if market and market['precision'] else 4 # 精度 (小数点以下の桁数)
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
            type='limit',
            side='sell',
            amount=amount_to_sell,
            price=take_profit,
            params={'timeInForce': 'GTC'} # GTC (Good-Til-Canceled)
        )
        tp_order_id = tp_order['id']
        logging.info(f"✅ TP指値売り注文成功: {symbol} @ {format_price_precision(take_profit)} (ID: {tp_order_id})")
    except Exception as e:
        logging.error(f"❌ TP指値売り注文設定失敗: {symbol} - {e}")
        # TP設定失敗時は、SLを設定せず、即座にポジションをクローズ（リスクを負わない）
        return {'status': 'error', 'error_message': f'TP注文設定失敗: {e}'}

    # 2. SL (ストップ指値) 売り注文の設定 (Stop Limit Sell)
    try:
        # SLトリガー価格と指値価格を設定。指値価格はトリガー価格より少し低く設定するのが一般的 (例: 0.1%下)
        stop_price = stop_loss
        limit_price = stop_loss * 0.999 # SL価格より0.1%下の指値価格
        
        # 数量の丸め (ここでは filled_amount をそのまま使用)
        amount_to_sell = filled_amount
        
        # ストップ指値売り注文 (Stop Limit Sell)
        sl_order = await EXCHANGE_CLIENT.create_order(
            symbol=symbol,
            type='limit', # 取引所によっては 'stop' や 'stop_limit'
            side='sell',
            amount=amount_to_sell,
            price=limit_price, # 実際に取引所に出される指値価格
            params={
                'stopPrice': stop_price, # ストップが発動する価格 (CCXT標準形式)
                'timeInForce': 'GTC',
            }
        )
        sl_order_id = sl_order['id']
        logging.info(f"✅ SLストップ指値売り注文成功: {symbol} (Trigger: {format_price_precision(stop_price)}, Limit: {format_price_precision(limit_price)}) (ID: {sl_order_id})")
        
    except Exception as e:
        logging.error(f"❌ SLストップ指値売り注文設定失敗: {symbol} - {e}")
        # SL設定失敗時は、TP注文をキャンセルし、即座にポジションをクローズ（リスクを負わない）
        if tp_order_id:
            try:
                await EXCHANGE_CLIENT.cancel_order(tp_order_id, symbol)
                logging.warning(f"⚠️ SL失敗のため、TP注文 (ID: {tp_order_id}) をキャンセルしました。")
            except Exception as cancel_e:
                 logging.error(f"❌ TPキャンセル失敗: {cancel_e}")
                 
        return {'status': 'error', 'error_message': f'SL注文設定失敗: {e}'}

    # 3. 成功
    return {
        'status': 'ok',
        'sl_order_id': sl_order_id,
        'tp_order_id': tp_order_id,
        'message': 'SL/TP注文が正常に設定されました。'
    }

async def close_position_immediately(symbol: str, filled_amount: float) -> Dict:
    """
    現物ポジションを成行で即座にクローズする。
    Returns: {'status': 'ok', 'closed_amount': amount} or {'status': 'error', 'error_message': '...'}
    """
    global EXCHANGE_CLIENT
    
    if filled_amount <= 0.0:
        return {'status': 'skipped', 'error_message': '約定数量がゼロのためクローズスキップ'}

    logging.warning(f"⚠️ リスク回避のため、{symbol} の {filled_amount:.4f} を即時成行でクローズを試みます。")
    
    try:
        # 成行売り注文
        close_order = await EXCHANGE_CLIENT.create_order(symbol, 'market', 'sell', filled_amount)
        
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
        order = await EXCHANGE_CLIENT.create_order(
            symbol=symbol, 
            type='limit', 
            side='buy', 
            amount=base_amount_to_buy, 
            price=entry_price,
            # ★ FOKからIOCへ変更 (v19.0.45 修正点)
            params={'timeInForce': 'IOC'} 
        )
        
        # 4. 注文結果の確認
        # IOCの場合、filled, remaining, statusが返される。
        filled_amount = order.get('filled', 0.0)
        
        # 💡 v19.0.47 修正: 約定を確実に検出
        if filled_amount > 0.0:
            # 即時約定成功 (部分約定または全約定)
            
            filled_usdt = order.get('cost', filled_amount * entry_price) # filled * average (取引所決済完了)"
            
            # averageがNoneの場合はlimit_priceを使用
            avg_entry_price = order.get('average') if order.get('average') is not None else entry_price
            
            logging.info(f"✅ IOC注文成功 ({symbol}): 約定価格={format_price_precision(avg_entry_price)}, 約定数量={filled_amount:.4f}, コスト={format_usdt(filled_usdt)} USDT")
            
            # SL/TP注文の設定
            sl_tp_result = await place_sl_tp_orders(
                symbol=symbol,
                filled_amount=filled_amount, # 約定した数量のみSL/TPを設定
                stop_loss=signal['stop_loss'],
                take_profit=signal['take_profit']
            )
            
            if sl_tp_result['status'] == 'ok':
                # ポジションをグローバルリストに追加
                OPEN_POSITIONS.append({
                    'id': str(uuid.uuid4()), # ボットが管理するユニークID
                    'symbol': symbol,
                    'timeframe': signal['timeframe'],
                    'entry_price': avg_entry_price,
                    'stop_loss': signal['stop_loss'],
                    'take_profit': signal['take_profit'],
                    'filled_amount': filled_amount,
                    'filled_usdt': filled_usdt,
                    'sl_order_id': sl_tp_result['sl_order_id'],
                    'tp_order_id': sl_tp_result['tp_order_id'],
                    'entry_time': time.time(),
                })

                return {
                    'status': 'ok',
                    'filled_amount': filled_amount,
                    'filled_usdt': filled_usdt,
                    'entry_price': avg_entry_price,
                    'id': order['id'], # 買い注文のID
                    'sl_order_id': sl_tp_result['sl_order_id'],
                    'tp_order_id': sl_tp_result['tp_order_id'],
                    'message': f"現物指値買い注文が即時約定しました。SL/TP注文を設定済み (ID: {order['id']})"
                }
            else:
                # 🔴 SL/TP注文設定に失敗した場合
                logging.error("❌ IOC約定後のSL/TP注文設定に失敗しました。リスク回避のためポジションを即時クローズします。")
                
                # SL/TP設定に失敗した場合、リスク回避のため即座にポジションを成行でクローズする
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
        # この場合、取引所への問い合わせが必要だが、ここでは簡略化のため強制クローズを試みる
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
        logging.critical(f"❌ 取引実行中に予期せぬエラーが発生 ({symbol}): {error_message}", exc_info=True)
        return {'status': 'error', 'error_message': error_message, 'close_status': 'skipped'}


async def cancel_all_related_orders(position: Dict, open_order_ids: List[str]):
    """特定のポジションに関連するすべてのオープン注文をキャンセルする"""
    global EXCHANGE_CLIENT
    
    symbol = position['symbol']
    orders_to_cancel = []
    
    # 1. ボットがトラッキングしているSL/TP注文
    if position['sl_order_id'] in open_order_ids:
        orders_to_cancel.append(position['sl_order_id'])
    if position['tp_order_id'] in open_order_ids:
        orders_to_cancel.append(position['tp_order_id'])
        
    for order_id in orders_to_cancel:
        try:
            await EXCHANGE_CLIENT.cancel_order(order_id, symbol)
            logging.info(f"✅ 関連注文をキャンセルしました: {symbol} (ID: {order_id})")
        except Exception as e:
            # すでに約定/キャンセルされている可能性あり
            logging.warning(f"⚠️ 注文のキャンセルに失敗 (ID: {order_id}, Symbol: {symbol}): {e}")

async def open_order_management_loop():
    """オープン注文とポジションの状態を監視するループ (10秒ごと)"""
    global EXCHANGE_CLIENT, OPEN_POSITIONS, GLOBAL_MACRO_CONTEXT
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.warning("⚠️ 注文監視スキップ: CCXTクライアントが未準備です。")
        return

    if not OPEN_POSITIONS:
        logging.debug("ℹ️ 注文監視スキップ: 管理対象のオープンポジションがありません。")
        return

    try:
        logging.debug(f"⏳ オープン注文監視ループ開始: {len(OPEN_POSITIONS)} ポジションをチェック中。")
        
        positions_to_remove_ids = []
        open_order_ids = []

        # 💡 【MEXC対応修正】CCXTは取引所によって `fetchOpenOrders` の動作が異なる
        # MEXCはシンボル引数を必須とするため、全てのシンボルに対して個別に注文を取得する
        symbols_to_check = list(set(p['symbol'] for p in OPEN_POSITIONS))
        
        for symbol in symbols_to_check:
            try:
                # 特定のシンボルのオープン注文を取得
                orders = await EXCHANGE_CLIENT.fetch_open_orders(symbol=symbol)
                
                # 取得した注文IDをグローバルなオープンリストに追加
                open_order_ids.extend([order['id'] for order in orders])
                
            except Exception as e:
                # 個別シンボルでの取得に失敗した場合は警告を出すが、他のシンボルは継続
                logging.warning(f"⚠️ {symbol} のオープン注文取得に失敗: {e}")
                
        # 監視対象のポジションをチェック
        for position in OPEN_POSITIONS:
            
            is_closed = False
            exit_type = None
            
            sl_open = position['sl_order_id'] in open_order_ids
            tp_open = position['tp_order_id'] in open_order_ids
            
            # SL/TP注文が両方ともオープン注文リストから消えているかチェック
            if not sl_open and not tp_open:
                is_closed = True
                exit_type = "取引所決済完了"
                logging.info(f"🔴 決済検出: {position['symbol']} - SL/TP注文が取引所から消滅。決済完了と見なします。")

            elif sl_open and tp_open:
                # 決済注文が両方とも残っている = ポジションオープン中
                logging.debug(f"ℹ️ {position['symbol']} は引き続きオープン中 (SL: {sl_open}, TP: {tp_open})")
                pass
            else:
                # 片方のみが残っている場合（取引所の自動キャンセルに失敗）は、一旦オープン中として扱う
                logging.warning(f"⚠️ {position['symbol']} は片方の決済注文が消滅 (SL:{sl_open}, TP:{tp_open})。自動キャンセル失敗の可能性あり。")
                pass
                
            if is_closed:
                positions_to_remove_ids.append(position['id'])
                
                # 約定価格は履歴から取得が必要だが、ここでは簡略化のため0.0とする
                closed_result = {
                    'symbol': position['symbol'],
                    'entry_price': position['entry_price'],
                    'stop_loss': position['stop_loss'],
                    'take_profit': position['take_profit'],
                    'exit_price': 0.0, # 約定価格は履歴から取得が必要だが、ここでは省略
                    'filled_amount': position['filled_amount'],
                    'exit_type': exit_type,
                    'pnl_usdt': None, # PnLは履歴から取得が必要
                    'pnl_rate': None,
                }
                
                # 通知
                current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
                # ★ v19.0.46 修正点: exit_typeを引数として渡す
                notification_message = format_telegram_message(closed_result, "ポジション決済", current_threshold, closed_result, exit_type=exit_type)
                await send_telegram_notification(notification_message)
                log_signal(closed_result, "Position Exit")
                
                # 残った未約定注文をキャンセル (念のため)
                await cancel_all_related_orders(position, open_order_ids)
                
    except Exception as e:
        logging.error(f"❌ オープン注文監視中にエラーが発生: {e}")
        
    finally:
        # 監視リストから決済されたポジションを削除
        OPEN_POSITIONS = [p for p in OPEN_POSITIONS if p['id'] not in positions_to_remove_ids]

# ====================================================================================
# NEW ASYNC ANALYSIS LOGIC (V19.0.44/V19.0.45)
# ====================================================================================

async def analyze_symbol(symbol: str, account_status: Dict) -> Tuple[str, List[Dict], Optional[str]]:
    """
    単一の銘柄のOHLCVデータを取得し、全時間足で分析してシグナルリストを返す非同期関数。
    成功した場合は (symbol, list_of_signals, None) を返し、失敗した場合は (symbol, [], error_message) を返す。
    """
    
    # 処理中のポジションのシンボルリスト
    open_position_symbols_only = [p['symbol'] for p in OPEN_POSITIONS]

    # 1. 銘柄がポジション保有中の場合はスキップ
    if symbol in open_position_symbols_only:
        return symbol, [], 'Position Open (Skipped)'
             
    # 2. クールダウンチェック
    if symbol in LAST_SIGNAL_TIME and (time.time() - LAST_SIGNAL_TIME[symbol] < TRADE_SIGNAL_COOLDOWN):
        return symbol, [], 'Cooldown (Skipped)'
        
    symbol_signals: List[Dict] = []
    
    try:
        # 最新のTicker情報を取得
        market_ticker = await EXCHANGE_CLIENT.fetch_ticker(symbol)
        
        # 全ての時間足のデータ取得と分析を並列で実行
        tasks = []
        for tf in TARGET_TIMEFRAMES:
            # fetch_ohlcv_and_analyze は後で定義するヘルパー関数
            tasks.append(
                fetch_ohlcv_and_analyze(symbol, tf, market_ticker)
            )
            
        # 全時間足の分析結果を収集
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # 結果を処理
        for result in results:
            if isinstance(result, Exception):
                # 個別時間足のデータ取得・分析エラー (致命的ではない)
                logging.warning(f"⚠️ {symbol} ({tf} Analysys Error): {result}")
                continue
            
            if result is not None:
                # 成功したシグナル
                # ロットサイズを計算して追加
                result['lot_size_usdt'] = calculate_dynamic_lot_size(result['score'], account_status)
                symbol_signals.append(result)

    except ccxt.RateLimitExceeded as e:
        logging.error(f"❌ {symbol} のAPIレート制限超過: {e}")
        return symbol, [], f'API Rate Limit Exceeded'
    except Exception as e:
        logging.error(f"❌ {symbol} の分析中に予期せぬエラーが発生: {e}")
        return symbol, [], f'Unexpected Error during analysis: {e}'

    # シグナルが1つでもあれば成功
    if symbol_signals:
        return symbol, symbol_signals, None
    else:
        # データ取得/指標計算は成功したが、有効なスコア（ATR/SL/TP計算が成立したもの）が得られなかった場合
        # この場合も分析成功と見なすが、シグナルリストは空として返す
        return symbol, [], 'No Valid Score Generated (Data insufficient for ATR/SL/TP)'


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
    GLOBAL_MACRO_CONTEXT = await fetch_fgi_data() # FGIの値をスコアリングに反映する準備
    
    macro_influence_score = (GLOBAL_MACRO_CONTEXT.get('fgi_proxy', 0.0) * FGI_PROXY_BONUS_MAX + GLOBAL_MACRO_CONTEXT.get('forex_bonus', 0.0) * FGI_PROXY_BONUS_MAX) * 100
    
    # 動的取引閾値の取得
    current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
    
    logging.info(f"📊 動的取引閾値: {current_threshold*100:.2f} / 100 (マクロ影響: {macro_influence_score:.2f} 点)")

    # 2. 口座ステータスの取得 (総資産額、USDT残高)
    account_status = await fetch_account_status()
    
    # 3. 監視銘柄リストの更新 (1時間に一度など、頻度は調整可能)
    if time.time() - LAST_SUCCESS_TIME > 60 * 60 or not IS_FIRST_MAIN_LOOP_COMPLETED:
        if not SKIP_MARKET_UPDATE:
            top_symbols = await fetch_top_symbols()
            # 既に保有中のポジションのシンボルは監視対象に含める
            open_position_symbols = [p['symbol'] for p in OPEN_POSITIONS]
            # 出来高TOPとポジションを組み合わせる
            updated_symbols = list(set(top_symbols + DEFAULT_SYMBOLS + open_position_symbols))
            CURRENT_MONITOR_SYMBOLS = updated_symbols
            logging.info(f"✅ 監視対象銘柄リストを更新しました。合計 {len(CURRENT_MONITOR_SYMBOLS)} 銘柄。")
        else:
             logging.info("ℹ️ 監視対象銘柄リストの更新はSKIP_MARKET_UPDATEによりスキップされました。")


    # 4. 全ての監視銘柄に対してシグナルを生成し、スコアリング (並列実行)
    all_signals: List[Dict] = []
    analysis_tasks = [analyze_symbol(symbol, account_status) for symbol in CURRENT_MONITOR_SYMBOLS]
    
    # 並列実行
    analysis_results = await asyncio.gather(*analysis_tasks)

    for symbol, symbol_signals, failure_reason in analysis_results:
        if failure_reason:
            # 失敗またはスキップ (クールダウン/ポジション保有/APIエラーなど)
            # HOURLY_ATTEMPT_LOGに記録 (このリストは成功したシグナルを排除したものになる)
            if symbol not in HOURLY_ATTEMPT_LOG: # 既に記録されている場合はスキップ
                HOURLY_ATTEMPT_LOG[symbol] = failure_reason
        else:
            # 分析成功 (シグナルリストは空の場合もある - 有効なスコアが一つも生成されなかった場合)
            all_signals.extend(symbol_signals)

    # 5. シグナルの評価と取引の実行
    
    # スコアで降順にソート
    all_signals.sort(key=lambda x: x['score'], reverse=True)
    
    LAST_ANALYSIS_SIGNALS = all_signals.copy()
    
    # HOURLY_SIGNAL_LOGに、スコアリングされた全てのシグナル（閾値未満も含む）を記録します。
    HOURLY_SIGNAL_LOG.extend(all_signals) 

    if all_signals:
        best_signal = all_signals[0]
        
        # 動的ロットサイズを再計算 (最高スコアに基づいて)
        # analyze_symbol内で一度計算済みだが、ここでは最新のaccount_statusで最終確認
        best_signal['lot_size_usdt'] = calculate_dynamic_lot_size(best_signal['score'], account_status)
        
        # 【取引の実行】 - TEST_MODEではない & クールダウンを過ぎている
        if not TEST_MODE and best_signal['score'] >= current_threshold and (best_signal['symbol'] not in LAST_SIGNAL_TIME or (time.time() - LAST_SIGNAL_TIME[best_signal['symbol']] >= TRADE_SIGNAL_COOLDOWN)):
            
            # 取引シグナルが閾値を超えているか
            score_met = best_signal['score'] >= current_threshold
            
            # 最低USDT残高があるか
            min_balance_met = account_status['total_usdt_balance'] >= MIN_USDT_BALANCE_FOR_TRADE
            
            # 取引実行結果を格納する辞書を初期化
            trade_result = None

            if score_met:
                if min_balance_met:
                    logging.info(f"🔥 取引シグナル発動: {best_signal['symbol']} - スコア {best_signal['score'] * 100:.2f} >= 閾値 {current_threshold*100:.2f}。取引を実行します。")
                    # 取引の実行
                    trade_result = await execute_trade(best_signal, account_status)
                else:
                    # スコアは満たしたが、残高不足
                    error_message = f"残高不足 (現在: {format_usdt(account_status['total_usdt_balance'])} USDT)。新規取引に必要な額: {MIN_USDT_BALANCE_FOR_TRADE:.2f} USDT。"
                    trade_result = {'status': 'error', 'error_message': error_message, 'close_status': 'skipped'}
                    logging.warning(f"⚠️ {best_signal['symbol']} 取引スキップ: {error_message}")
            else:
                logging.info(f"ℹ️ {best_signal['symbol']} は閾値 {current_threshold*100:.2f} を満たしていません。取引をスキップします。")

            # 6. Telegram通知
            if trade_result and trade_result.get('status') == 'ok':
                # 取引成功
                # ★ v19.0.47 修正点: BOT_VERSION を明示的に渡す
                notification_message = format_telegram_message(best_signal, "取引シグナル", current_threshold, trade_result)
                await send_telegram_notification(notification_message)
                log_signal(best_signal, "Signal Executed")
                LAST_SIGNAL_TIME[best_signal['symbol']] = time.time()
                
            elif trade_result and trade_result.get('status') == 'error':
                 # 取引失敗（API/残高不足/SLTP設定失敗/強制クローズ結果など）
                 # ★ v19.0.47 修正点: BOT_VERSION を明示的に渡す
                 notification_message = format_telegram_message(best_signal, "取引シグナル", current_threshold, trade_result)
                 await send_telegram_notification(notification_message)
                 log_signal(best_signal, "Signal Failed")
                 
            else:
                 # シグナルは出たが閾値未満で実行されなかった場合、ログに記録するのみ
                 log_signal(best_signal, "Signal Found (No Trade)")
                
        else:
            # TEST_MODE または クールダウン中の場合は、最高シグナルをログに記録
            log_signal(best_signal, "Signal Found (No Trade)")
    
    # 7. 1時間ごとのスコア通知レポート (★閾値に関わらず最高・最低スコアを報告します)
    if time.time() - LAST_HOURLY_NOTIFICATION_TIME >= HOURLY_SCORE_REPORT_INTERVAL:
        logging.info("⏳ 1時間ごとのスコアレポートを生成中...")
        # HOURLY_SIGNAL_LOGが空の場合でも、format_hourly_report内で「分析銘柄なし」のレポートを生成する
        report_message = format_hourly_report(HOURLY_SIGNAL_LOG, HOURLY_ATTEMPT_LOG, LAST_HOURLY_NOTIFICATION_TIME, current_threshold, BOT_VERSION)
        await send_telegram_notification(report_message)
        
        HOURLY_SIGNAL_LOG = [] # リストをクリア
        HOURLY_ATTEMPT_LOG = {} # リストをクリア
        LAST_HOURLY_NOTIFICATION_TIME = time.time()
            
    # 8. 初回起動完了通知 (一度だけ)
    if not IS_FIRST_MAIN_LOOP_COMPLETED:
        # 初回起動通知
        startup_message = format_startup_message(account_status, GLOBAL_MACRO_CONTEXT, len(CURRENT_MONITOR_SYMBOLS), current_threshold, BOT_VERSION)
        await send_telegram_notification(startup_message)
        IS_FIRST_MAIN_LOOP_COMPLETED = True
        
    LAST_SUCCESS_TIME = time.time()
    
    end_time = time.time()
    logging.info(f"--- 💡 {datetime.now(JST).strftime('%Y/%m/%d %H:%M:%S')} - BOT LOOP END (Execution Time: {end_time - start_time:.2f}s) ---")


async def main_bot_scheduler():
    """メインBOTループを定期実行するスケジューラ (1分ごと)"""
    global BOT_VERSION
    # 初回起動後の待機時間を考慮し、初回は即座に実行を試みる
    await asyncio.sleep(5) 
    
    while True:
        try:
            await main_bot_loop()
        except Exception as e:
            # 致命的なエラーが発生した場合でも、ループを継続するためにエラーをログに記録し、待機時間を経て再試行
            logging.critical(f"❌ メインループ実行中に致命的なエラー: {e}", exc_info=True)
            try:
                 # ★ v19.0.47 修正点: BOT_VERSION を使用してエラー通知を強化
                 await send_telegram_notification(f"🚨 **致命的なエラー**\nメインループでエラーが発生しました: `{e}`\n(Bot Ver: {BOT_VERSION})")
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
# ★ v19.0.47 修正点: BOT_VERSION を使用
app = FastAPI(title="Apex BOT API", version=BOT_VERSION)

@app.on_event("startup")
async def startup_event():
    """アプリケーション起動時にCCXTクライアントを初期化し、メインのタスクを開始する"""
    logging.info("🚀 BOTの起動処理を開始します...")
    
    # CCXTクライアントの初期化
    await initialize_exchange_client()
    
    # メインBOTループの非同期タスクを開始
    asyncio.create_task(main_bot_scheduler())
    
    # オープン注文監視ループの非同期タスクを開始
    asyncio.create_task(open_order_management_scheduler())


if __name__ == "__main__":
    # uvicorn.run() でFastAPIアプリケーションを実行
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
