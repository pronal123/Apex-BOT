# ====================================================================================
# Apex BOT v19.0.53 (Patched) - FEATURE: Periodic SL/TP Re-Placing for Unmanaged Orders
#
# 改良・修正点:
# 1. 【SL/TP再設定】open_order_management_loop関数内に、SLまたはTPの注文が片方または両方欠けている場合に、
#    残っている注文をキャンセルし、SL/TP注文を再設定するロジックを追加。
# 2. 【IOC失敗診断維持】v19.0.52で追加したIOC失敗時診断ログを維持。
# 3. 【レポート表示修正】Hourly Reportの分析対象数計算ロジックを修正 (v19.0.53-p1)
# 4. 【通知強化】取引シグナル通知に推定損益(USDT)を表示する機能を追加 (v19.0.53-p1)
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
GLOBAL_EXCLUDED_SYMBOLS: List[str] = [] # シンボル除外リストの初期化 (前回のSyntaxError対応で追加)

# ★ 新規追加: ボットのバージョン (v19.0.53-p1: レポート修正＆推定損益表示版)
BOT_VERSION = "v19.0.53-p1"

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
SIGNAL_THRESHOLD_SLUMP = 0.86       # 88.00点 (リスクオフ時は厳しく)
SIGNAL_THRESHOLD_NORMAL = 0.84      # 86.00点 (ベースライン)
SIGNAL_THRESHOLD_ACTIVE = 0.80      # 83.00点 (リスクオン時は緩く)      

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

    # 💡 【追加】推定損益の計算ロジック
    est_pnl_line = ""
    if context == "取引シグナル" and entry_price > 0:
        # 数量(Amount)の確定: 約定していればその数量、なければシグナルの想定数量を使用
        calc_amount = 0.0
        if trade_result and trade_result.get('filled_amount', 0.0) > 0:
            calc_amount = trade_result['filled_amount']
        else:
            # まだ約定していない、またはテストモードの場合は想定ロットサイズから計算
            lot_usdt = signal.get('lot_size_usdt', BASE_TRADE_SIZE_USDT)
            calc_amount = lot_usdt / entry_price

        # SL/TPにかかった場合のUSDT差額を計算
        est_loss_usdt = (stop_loss - entry_price) * calc_amount
        est_profit_usdt = (take_profit - entry_price) * calc_amount
        
        # 表示用フォーマット
        est_pnl_line = f"  - **推定損益**: 🔴{format_usdt(est_loss_usdt)} / 🟢+{format_usdt(est_profit_usdt)} USDT\n"

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
                f"\n💰 **取引失敗詳細**\n"
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
        
        trade_status_line = f"🛑 **ポジション決済完了** ({exit_type_final})"
        pnl = trade_result.get('pnl_usdt', 0.0)
        pnl_percent = trade_result.get('pnl_percent', 0.0)
        
        # PnLの整形
        pnl_sign = "🟢" if pnl >= 0 else "🔴"
        pnl_text = f"{pnl_sign} <code>{format_usdt(pnl)}</code> USDT ({pnl_percent:+.2f}%)"
        
        trade_section = (
            f"💰 **取引決済結果**\n"
            f"  - **決済タイプ**: <code>{exit_type_final}</code>\n"
            f"  - **エントリー**: <code>{format_price_precision(entry_price)}</code>\n"
            f"  - **決済価格**: <code>{format_price_precision(trade_result.get('exit_price', 0.0))}</code>\n"
            f"  - **実現損益 (PnL)**: {pnl_text}\n"
            f"  - **総資産**: <code>{format_usdt(GLOBAL_TOTAL_EQUITY)}</code> USDT (更新)\n"
        )
    
    # メッセージ本体の組み立て
    message = (
        f"🔔 **{context}** - <b>{symbol}</b> ({timeframe})\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - **時刻**: {now_jst} (JST)\n"
        f"  - **スコア**: <code>{score*100:.2f} / 100</code> (閾値: {current_threshold*100:.2f})\n"
        f"  - **推定勝率**: <code>{estimated_wr}</code>\n"
        f"  - **エントリー (指値)**: <code>{format_price_precision(entry_price)}</code>\n"
        f"  - **リスクリワード (R:R)**: <code>1:{rr_ratio:.2f}</code>\n"
        f"  - **SL/TP**: <code>{format_price_precision(stop_loss)}</code> / <code>{format_price_precision(take_profit)}</code>\n"
        f"{est_pnl_line}" # ★ここで推定損益の行を追加
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"**{trade_status_line}**\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
    )

    # 💡 取引結果セクションを追加
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
    global DEFAULT_SYMBOLS
    
    now_jst = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")
    start_jst = datetime.fromtimestamp(start_time, JST).strftime("%H:%M:%S")
    
    # スコアでソート
    signals_sorted = sorted(signals, key=lambda x: x['score'], reverse=True)
    
    # 💡【修正】分析対象とスキップの計算ロジック
    analyzed_count = len(signals)
    skipped_count = len(attempt_log) # attempt_log はスキップされたもののみ
    total_attempted = analyzed_count + skipped_count # 総試行数
    
    message = (
        f"📈 **【Hourly Report】** - {start_jst} から {now_jst} (JST)\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - **分析対象**: <code>{total_attempted}</code> 銘柄 / <code>{len(DEFAULT_SYMBOLS) * len(TARGET_TIMEFRAMES)}</code> タイムフレーム\n"
        f"  - **有効シグナル**: <code>{analyzed_count}</code> 件 (スコア > {0.50*100:.0f})\n" # ベーススコア以上
        f"  - **スキップ**: <code>{skipped_count}</code> 銘柄 ({'、'.join(attempt_log.values()) if attempt_log else 'なし'})\n"
        f"  - **現在の取引閾値**: <code>{current_threshold*100:.0f} / 100</code>\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
    )
    
    if not signals_sorted:
        message += f"\n➖ **有効なシグナルは見つかりませんでした**\n"
        message += (
            f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
            f"<i>Bot Ver: {bot_version} - Full Analysis & Async Refactoring</i>"
        )
        return message

    # ベストシグナル (最高スコア)
    best_signal = signals_sorted[0]
    message += (
        f"\n👑 **Top 3 Best Signals**\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
    )
    for i, signal in enumerate(signals_sorted[:TOP_SIGNAL_COUNT]):
        est_wr = get_estimated_win_rate(signal['score'])
        message += (
            f"  - **{i+1}. {signal['symbol']}** ({signal['timeframe']}) "
            f"[Score: <code>{signal['score']*100:.2f}</code> | WR: {est_wr}]\n"
        )
        if i == 0:
            # ベストシグナルには詳細情報を追加
            message += (
                f"  - **エントリー (Entry)**: <code>{format_price_precision(signal['entry_price'])}</code>\n"
                f"  - **SL/TP**: <code>{format_price_precision(signal['stop_loss'])}</code> / <code>{format_price_precision(signal['take_profit'])}</code>\n"
            )
            
    # ワーストシグナル (最低スコア、ただしベースライン以上のもの)
    worst_signal = signals_sorted[-1]
    message += (
        f"\n🐞 **Worst Signal (min score {0.50*100:.0f})**\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - **{worst_signal['symbol']}** ({worst_signal['timeframe']}) "
        f"[Score: <code>{worst_signal['score']*100:.2f}</code> | WR: {get_estimated_win_rate(worst_signal['score'])}]\n"
        f"  - **エントリー (Entry)**: <code>{format_price_precision(worst_signal['entry_price'])}</code>\n"
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
        'symbol': signal.get('symbol'),
        'timeframe': signal.get('timeframe'),
        'score': signal.get('score'),
        'entry_price': signal.get('entry_price'),
        'stop_loss': signal.get('stop_loss'),
        'take_profit': signal.get('take_profit'),
        'rr_ratio': signal.get('rr_ratio'),
        'filled_usdt': signal.get('filled_usdt', signal.get('pnl_usdt')),
        'trade_result_status': signal.get('status', 'N/A'),
        'error_message': signal.get('error_message', 'N/A'),
        'pnl_percent': signal.get('pnl_percent', 'N/A'),
        # tech_dataは冗長になるため、ロギングからは除外
        #'tech_data': _to_json_compatible(signal.get('tech_data'))
    }
    
    # contextに応じてログレベルを調整
    if context == "ポジション決済" and log_data['pnl_percent'] is not None and log_data['pnl_percent'] < 0:
        # ロスカットまたはマイナス決済はWARN
        logging.warning(f"🔔 Signal Log: {json.dumps(log_data, ensure_ascii=False)}")
    elif log_data['trade_result_status'] == 'error':
        # 取引失敗はERROR
        logging.error(f"🔔 Signal Log: {json.dumps(log_data, ensure_ascii=False)}")
    else:
        # 通常の成功シグナル/決済はINFO
        logging.info(f"🔔 Signal Log: {json.dumps(log_data, ensure_ascii=False)}")

# ====================================================================================
# TELEGRAM NOTIFICATION
# ====================================================================================

async def send_telegram_notification(message: str):
    """Telegramにメッセージを送信する"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.warning("⚠️ TelegramのトークンまたはChat IDが設定されていません。通知をスキップします。")
        return

    # HTML形式でメッセージを送信
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        'chat_id': TELEGRAM_CHAT_ID,
        'text': message,
        'parse_mode': 'HTML'
    }
    
    # asyncio.to_threadを使用してブロッキング処理（requests）を非同期に実行
    # Note: requestsはブロッキングなので、asyncioの文脈で使う場合はto_threadを使う
    try:
        response = await asyncio.to_thread(requests.post, url, data=payload, timeout=10)
        response.raise_for_status() # HTTPエラーを確認
        logging.info(f"✅ Telegram通知送信成功。メッセージ長: {len(message)}")
    except requests.exceptions.RequestException as e:
        # ステータスコード400 (Bad Request)など、Telegram APIからのエラーをログに記録
        error_details = ""
        try:
            error_details = response.text
        except:
            pass
        logging.error(f"❌ Telegram通知送信失敗 (RequestError): {e}. Details: {error_details}")

# ====================================================================================
# CCXT WRAPPERS & DATA FETCHING
# ====================================================================================

def initialize_exchange_client():
    """CCXTクライアントを初期化する"""
    global EXCHANGE_CLIENT, IS_CLIENT_READY
    
    if IS_CLIENT_READY and EXCHANGE_CLIENT:
        return

    try:
        exchange_class = getattr(ccxt_async, CCXT_CLIENT_NAME.lower())
        
        # 💡 APIキー/シークレットが存在しない場合は、PUBLICクライアントとして初期化
        if not API_KEY or not SECRET_KEY:
            logging.warning(f"⚠️ {CCXT_CLIENT_NAME.upper()}_API_KEYまたはSECRETが未設定です。PUBLICクライアントとして初期化します。取引機能は無効です。")
            EXCHANGE_CLIENT = exchange_class({
                'enableRateLimit': True, 
                'rateLimit': 1000, # 1000ms/1回
            })
        else:
            EXCHANGE_CLIENT = exchange_class({
                'apiKey': API_KEY,
                'secret': SECRET_KEY,
                'enableRateLimit': True, 
                'rateLimit': 1000, # 1000ms/1回
            })

        # 接続テスト (load_markets はブロッキングなので、初回は非同期ループの外で実行するか、try/exceptでラップ)
        async def load_markets_async():
            if not SKIP_MARKET_UPDATE:
                await EXCHANGE_CLIENT.load_markets()

        # 非同期処理の初期化が完了するまで待機
        asyncio.run(load_markets_async())

        IS_CLIENT_READY = True
        logging.info(f"✅ CCXTクライアント ({CCXT_CLIENT_NAME.upper()}) 初期化成功。取引機能: {'ON' if API_KEY and SECRET_KEY else 'OFF'}")

    except AttributeError:
        logging.critical(f"❌ 取引所名 '{CCXT_CLIENT_NAME}' がCCXTに見つかりません。")
        sys.exit(1)
    except Exception as e:
        logging.critical(f"❌ CCXTクライアントの初期化中に致命的なエラーが発生: {e}", exc_info=True)
        # sys.exit(1) # Render環境ではクラッシュを避けるため、続行を試みる

async def fetch_ohlcv_and_analyze(symbol: str, timeframe: str, required_limit: int, market_tickers: Dict[str, Dict]) -> Optional[Dict]:
    """OHLCVデータを取得し、テクニカル分析を行う"""
    global EXCHANGE_CLIENT, GLOBAL_MACRO_CONTEXT, GLOBAL_EXCLUDED_SYMBOLS
    
    # 💡 シンボル除外リストのチェック (新しいロジック)
    if symbol in GLOBAL_EXCLUDED_SYMBOLS:
        return None

    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.warning(f"⚠️ {symbol} ({timeframe}): クライアントが準備できていません。")
        return None
        
    try:
        # OHLCVデータの取得
        ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(symbol, timeframe, limit=required_limit)
        
        if not ohlcv or len(ohlcv) < required_limit:
            # 出来高が低い、またはデータが不足している場合はスキップ
            # 💡 【追加】スキップログの記録
            if len(ohlcv) < required_limit:
                GLOBAL_EXCLUDED_SYMBOLS.append(symbol) # データ不足は一時的なものなので、永続的な除外はしない
                logging.warning(f"⚠️ {symbol} ({timeframe}): OHLCVデータが不足しています ({len(ohlcv)}/{required_limit})。次回以降この銘柄はスキップします。")
                HOURLY_ATTEMPT_LOG[symbol] = "データ不足"
            return None

        # pandas DataFrameに変換
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['datetime'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True).dt.tz_convert(JST)
        
        # インジケータの計算
        df = calculate_indicators(df)
        
        # シグナルとスコアの計算
        signal = calculate_score_and_signal(symbol, timeframe, df, market_tickers, GLOBAL_MACRO_CONTEXT)

        return signal

    except ccxt.DDoSProtection as e:
        logging.warning(f"⚠️ {symbol} ({timeframe}): DDoS保護によりスキップされました。レート制限を遵守します。")
        HOURLY_ATTEMPT_LOG[symbol] = "レート制限"
        return None
    except ccxt.ExchangeNotAvailable as e:
        logging.warning(f"⚠️ {symbol} ({timeframe}): 取引所が利用不能です。スキップします。")
        HOURLY_ATTEMPT_LOG[symbol] = "取引所ダウン"
        return None
    except ccxt.BadSymbol as e:
        logging.warning(f"⚠️ {symbol} ({timeframe}): シンボルが不正または上場廃止です。この銘柄を除外します。")
        if symbol not in GLOBAL_EXCLUDED_SYMBOLS:
            GLOBAL_EXCLUDED_SYMBOLS.append(symbol)
            HOURLY_ATTEMPT_LOG[symbol] = "不正シンボル"
        return None
    except Exception as e:
        # このエラーは analyze_symbol でキャッチされる
        raise Exception(f"OHLCV Fetch/Indicator Calc Error for {symbol} ({tf}): {e}")


async def fetch_account_status() -> Dict:
    """現在のUSDT残高と総資産額を取得する"""
    global EXCHANGE_CLIENT, GLOBAL_TOTAL_EQUITY
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        return {'total_usdt_balance': 0.0, 'total_equity': 0.0, 'open_positions': [], 'error': True}

    if not API_KEY or not SECRET_KEY:
        # 公開クライアントの場合、残高情報は取得できない
        logging.warning("⚠️ APIキーが設定されていません。口座ステータスはゼロとして処理されます。")
        return {'total_usdt_balance': 0.0, 'total_equity': 0.0, 'open_positions': [], 'error': True}

    try:
        # 残高の取得
        balance_data = await EXCHANGE_CLIENT.fetch_balance()
        total_usdt_balance = balance_data.get('free', {}).get('USDT', 0.0)
        
        # 総資産額の計算 (USDT残高 + 他の保有資産のUSDT評価額)
        total_equity = total_usdt_balance
        balance = balance_data
        
        # USDT以外の保有資産を評価 (total balanceを使用)
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
                        total_equity += usdt_value
                        
                except Exception as e:
                    # エラーが発生した場合、その通貨の評価はスキップ
                    logging.warning(f"⚠️ {currency} のUSDT価値を取得できませんでした（{EXCHANGE_CLIENT.name} GET {symbol}）。")

        GLOBAL_TOTAL_EQUITY = total_equity # グローバル変数も更新
        logging.info(f"✅ 口座ステータス取得成功: Equity={format_usdt(GLOBAL_TOTAL_EQUITY)} USDT, Free USDT={format_usdt(total_usdt_balance)}")
        
        # USDT以外の保有資産の評価 (通知用)
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
    global EXCHANGE_CLIENT, DEFAULT_SYMBOLS
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        return DEFAULT_SYMBOLS # クライアントが未準備の場合はデフォルトリストを返す

    try:
        # マーケットデータをロード
        if EXCHANGE_CLIENT.markets is None or not EXCHANGE_CLIENT.markets:
            await EXCHANGE_CLIENT.load_markets()

        # USDS/USDTの現物ペアのみをフィルタリング
        spot_usdt_symbols = [
            s for s, market in EXCHANGE_CLIENT.markets.items() 
            if (s.endswith('/USDT') or s.endswith('USDT')) and market.get('spot') and market.get('active', True)
        ]
        
        # 出来高トップのティッカー情報を取得 (一部の取引所では非対応のため、try/exceptでラップ)
        if hasattr(EXCHANGE_CLIENT, 'fetch_tickers') and callable(EXCHANGE_CLIENT.fetch_tickers):
            try:
                tickers = await EXCHANGE_CLIENT.fetch_tickers(spot_usdt_symbols)
                # 24時間出来高 (quote volume) でソート
                # 'quoteVolume'がない場合は'baseVolume'を使用するが、ここではUSDTペアなので'quoteVolume'を優先
                sorted_tickers = sorted(
                    [t for t in tickers.values() if t and t.get('quoteVolume')],
                    key=lambda t: t['quoteVolume'],
                    reverse=True
                )
                
                # TOP_SYMBOL_LIMITまでのシンボルを取得
                top_symbols = [t['symbol'] for t in sorted_tickers[:TOP_SYMBOL_LIMIT]]
                
                # デフォルトシンボルから、まだトップリストにない主要通貨を追加
                # BTC/USDT, ETH/USDT, SOL/USDT, BNB/USDT は最低限監視したい
                for default_symbol in DEFAULT_SYMBOLS:
                    if default_symbol not in top_symbols:
                        top_symbols.append(default_symbol)
                        
                logging.info(f"✅ 出来高トップ銘柄リストを更新 ({len(top_symbols)} 銘柄)。トップ: {top_symbols[:5]}...")
                return top_symbols
                
            except Exception as e:
                logging.warning(f"⚠️ 出来高トップのティッカー情報取得失敗: {e}。デフォルトリストを使用します。")
                return DEFAULT_SYMBOLS

        return DEFAULT_SYMBOLS # fetch_tickersがない取引所の場合
        
    except Exception as e:
        logging.error(f"❌ 銘柄リスト更新中にエラーが発生: {e}", exc_info=True)
        return DEFAULT_SYMBOLS

async def fetch_fgi_data() -> Dict:
    """外部APIからFGI (恐怖・貪欲指数) と為替レートボーナスを取得する"""
    fgi_proxy = 0.0
    fgi_raw_value = 'N/A'
    forex_bonus = 0.0
    
    try:
        # 1. 恐怖・貪欲指数 (FGI) を取得
        fgi_url = "https://api.alternative.me/fng/?limit=1"
        fgi_response = await asyncio.to_thread(requests.get, fgi_url, timeout=5)
        fgi_response.raise_for_status()
        fgi_data = fgi_response.json().get('data', [])
        
        if fgi_data:
            value = int(fgi_data[0]['value'])
            fgi_raw_value = f"{fgi_data[0]['value_classification']} ({value})"
            
            # FGIを-0.05から+0.05の範囲に正規化（50は0.0）
            # Min=0 (Extreme Fear) -> -0.05, Max=100 (Extreme Greed) -> +0.05
            fgi_proxy = ((value - 50) / 100) * 2 * FGI_PROXY_BONUS_MAX
            
            logging.info(f"✅ FGIデータ取得: {fgi_raw_value}, Proxy Score: {fgi_proxy:.4f}")
            
        # 2. ドル円 (USD/JPY) を取得し、為替変動をボーナスとして追加
        # (ここでは簡略化のため、ダミーとして0.005の固定ボーナスを仮定)
        # 本来はUSD/JPYが急上昇/急落している場合にトレンドボーナスを適用
        # forex_bonus = 0.005 # 仮の固定値
        
    except requests.exceptions.RequestException as e:
        logging.error(f"❌ FGIデータ取得中にエラーが発生: {e}")
        # 失敗した場合は、全て0.0を返す
        pass

    return {
        'fgi_proxy': fgi_proxy,
        'fgi_raw_value': fgi_raw_value,
        'forex_bonus': forex_bonus,
    }

# ====================================================================================
# TECHNICAL ANALYSIS & SCORING LOGIC
# ====================================================================================

def calculate_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """テクニカル指標を計算し、DataFrameに追加する"""
    # 既存のcolumnsを上書きしないように、計算前にコピーを作成
    # df = df.copy() # 既にfetch_ohlcv_and_analyzeでコピー済み
    
    # Simple Moving Averages (SMA)
    df['SMA_50'] = ta.sma(df['close'], length=50)
    df['SMA_200'] = ta.sma(df['close'], length=LONG_TERM_SMA_LENGTH) # 200

    # Relative Strength Index (RSI)
    df['RSI'] = ta.rsi(df['close'], length=14)

    # Moving Average Convergence Divergence (MACD)
    macd_data = ta.macd(df['close'], fast=12, slow=26, signal=9, append=False)
    # MACDのデフォルトキーは 'MACD_12_26_9', 'MACDh_12_26_9', 'MACDs_12_26_9'
    if macd_data is not None and not macd_data.empty:
        df['MACD'] = macd_data.iloc[:, 0] # MACD line
        df['MACDh'] = macd_data.iloc[:, 1] # MACD histogram
        df['MACDs'] = macd_data.iloc[:, 2] # MACD signal line
    else:
        # MACD計算失敗時のフォールバック
        df['MACD'] = np.nan
        df['MACDh'] = np.nan
        df['MACDs'] = np.nan

    # Bollinger Bands (BBANDS) - 20期間, 2.0標準偏差
    bb_data = ta.bbands(df['close'], length=20, std=2.0, append=False)
    # ★★★ 修正箇所: KeyError: 'BBL_20_2.0' の修正 ★★★
    # pandas_taのバージョンアップにより、BBANDSのキーが 'BBL_20_2.0' -> 'BBL_20_2.0_2.0' に変更されました。
    bb_keys = [col for col in bb_data.columns if col.startswith('BBL_')]
    if bb_data is not None and not bb_data.empty and bb_keys:
        bb_prefix = bb_keys[0].split('_')[0]
        df['BBL'] = bb_data[f'{bb_prefix}_20_2.0']
        df['BBM'] = bb_data[f'{bb_prefix}_20_2.0']
        df['BBU'] = bb_data[f'{bb_prefix}_20_2.0']
        # BBW (バンド幅)
        df['BBW'] = (df['BBU'] - df['BBL']) / df['BBM']
    else:
        df['BBL'] = np.nan
        df['BBM'] = np.nan
        df['BBU'] = np.nan
        df['BBW'] = np.nan
        
    # Average True Range (ATR)
    df['ATR'] = ta.atr(df['high'], df['low'], df['close'], length=14)

    # On-Balance Volume (OBV)
    df['OBV'] = ta.obv(df['close'], df['volume'])
    df['OBV_SMA'] = ta.sma(df['OBV'], length=20) # OBVのSMA

    # Parabolic SAR
    df['SAR'] = ta.sar(df['high'], df['low'], af0=0.02, afstep=0.02, afmax=0.2)

    return df.dropna(subset=['close']) # NaN行を削除

def calculate_score_and_signal(symbol: str, timeframe: str, df: pd.DataFrame, market_tickers: Dict[str, Dict], macro_context: Dict) -> Optional[Dict]:
    """テクニカルデータに基づいてスコアを計算し、取引シグナルを作成する"""
    
    # 必要なデータが揃っているか確認
    if df.empty or len(df) < 2:
        return None
    
    last_candle = df.iloc[-1]
    prev_candle = df.iloc[-2] # 1つ前の確定足
    
    current_price_source = market_tickers.get(symbol)
    if not current_price_source:
        return None
    current_price = current_price_source.get('last')
    
    if pd.isna(last_candle['ATR']) or last_candle['ATR'] <= 0:
        # ATRがない、またはゼロの場合は有効なシグナルと見なさない
        return None

    # ====================================================================================
    # 1. トレンド/構造分析 (ロングを前提)
    # ====================================================================================

    # 長期トレンドフィルタ: 価格がSMA200の上にあるか
    is_above_long_term_sma = last_candle['close'] > last_candle['SMA_200']
    
    # 中期トレンドフィルタ: SMA50がSMA200の上にあるか
    is_mid_term_uptrend = last_candle['SMA_50'] > last_candle['SMA_200']

    # MACD: MACDラインがシグナルラインの上にあるか (MACD > MACDs)
    is_macd_uptrend = last_candle['MACD'] > last_candle['MACDs']
    
    # MACDヒストグラム: 直前のヒストグラムより現在の方が高いか (モメンタム加速)
    is_macd_momentum_accelerating = last_candle['MACDh'] > prev_candle['MACDh']
    
    # RSI: 買われすぎ/売られすぎではないか
    is_rsi_neutral = RSI_MOMENTUM_LOW <= last_candle['RSI'] <= 70

    # OBV: OBVがSMAの上にあるか (出来高による確証)
    is_obv_confirming = last_candle['OBV'] > last_candle['OBV_SMA']
    
    # パラボリックSAR: SARが価格の下にあるか (上昇トレンド)
    is_sar_uptrend = last_candle['SAR'] < last_candle['close']
    
    # 価格構造: 直前のローソク足が大きな陽線（ピンバー）であるか (リバーサルシグナル)
    # 陽線であり、かつ終値が始値から一定の割合以上離れている
    is_strong_reversal_candle = (
        last_candle['close'] > last_candle['open'] and 
        (last_candle['close'] - last_candle['open']) / last_candle['open'] > 0.005 # 0.5%以上の陽線
    )
    
    # ====================================================================================
    # 2. エントリーポイントとリスク管理の決定 (ATRベース)
    # ====================================================================================
    
    # エントリー価格: 現在の価格をそのまま使用 (指値IOCを想定するため)
    entry_price = current_price 

    # ストップロス (SL): ATRのn倍を使用
    # ATRの2.0倍を下回る価格をSLとする
    atr_multiplier = 2.0
    stop_loss = entry_price - (last_candle['ATR'] * atr_multiplier)
    
    # テイクプロフィット (TP): RR=1.5として、SL幅の1.5倍をTPとする
    rr_target = 1.5
    profit_target_distance = entry_price - stop_loss
    take_profit = entry_price + (profit_target_distance * rr_target)
    
    # SL/TPの妥当性チェック
    if stop_loss <= 0 or take_profit <= entry_price:
        # logging.warning(f"⚠️ {symbol} ({timeframe}): SL/TP計算が無効です (SL={stop_loss:.4f}, TP={take_profit:.4f})")
        return None

    # 4. スコアリング
    total_score = 0.0
    tech_data = {} # スコア詳細記録用

    # A. ベーススコア (50点)
    total_score += BASE_SCORE

    # B. 長期トレンド逆行ペナルティ (30点)
    # 価格がSMA200から大きく下回っている場合にペナルティ
    long_term_reversal_penalty_value = 0.0
    if not is_above_long_term_sma:
        # SMA200を下回っている場合、価格とSMA200の乖離率に応じてペナルティを適用
        deviation_ratio = (last_candle['SMA_200'] - last_candle['close']) / last_candle['SMA_200']
        # 乖離が大きくなるほどペナルティも大きくなる (最大でLONG_TERM_REVERSAL_PENALTY)
        # 乖離率が0.02 (2%)を超えると最大ペナルティ
        max_deviation = 0.02
        penalty_factor = min(deviation_ratio / max_deviation, 1.0)
        long_term_reversal_penalty_value = LONG_TERM_REVERSAL_PENALTY * penalty_factor
        
        total_score -= long_term_reversal_penalty_value
        
    tech_data['long_term_reversal_penalty_value'] = long_term_reversal_penalty_value

    # C. 中期/長期トレンドアライメントボーナス (10点)
    # SMA50がSMA200の上にある (中期的な上昇トレンドの確認)
    trend_alignment_bonus_value = 0.0
    if is_mid_term_uptrend:
        trend_alignment_bonus_value = TREND_ALIGNMENT_BONUS
        
    total_score += trend_alignment_bonus_value
    tech_data['trend_alignment_bonus_value'] = trend_alignment_bonus_value

    # D. 価格構造/ピボット支持ボーナス (6点)
    # パラボリックSARが価格の下にある、かつ強いリバーサルシグナルがある
    structural_pivot_bonus = 0.0
    if is_sar_uptrend and is_strong_reversal_candle:
        structural_pivot_bonus = STRUCTURAL_PIVOT_BONUS
        
    total_score += structural_pivot_bonus
    tech_data['structural_pivot_bonus'] = structural_pivot_bonus

    # E. MACDペナルティ (25点)
    # MACDがシグナルラインを下回っている (不利なクロス) か、モメンタムが減速している
    macd_penalty_value = 0.0
    if not is_macd_uptrend or not is_macd_momentum_accelerating:
        # 不利なMACDクロスの場合、ペナルティを適用
        macd_penalty_value = MACD_CROSS_PENALTY
        
        total_score -= macd_penalty_value

    tech_data['macd_penalty_value'] = macd_penalty_value

    # F. RSIモメンタムボーナス (10点)
    # RSIがRSI_MOMENTUM_LOW以下の場合、モメンタム回復への期待としてボーナスを与える (可変)
    rsi_momentum_bonus_value = 0.0
    rsi_value = last_candle['RSI']
    tech_data['rsi_value'] = rsi_value

    if rsi_value <= RSI_MOMENTUM_LOW:
        # RSIが低いほどボーナスが増加 (RSI=30で最大ボーナス)
        min_rsi = 30
        range_rsi = RSI_MOMENTUM_LOW - min_rsi
        
        if rsi_value < min_rsi:
            rsi_momentum_bonus_value = RSI_MOMENTUM_BONUS_MAX
        else:
            # 線形補間
            factor = (RSI_MOMENTUM_LOW - rsi_value) / range_rsi
            rsi_momentum_bonus_value = RSI_MOMENTUM_BONUS_MAX * factor
            
        total_score += rsi_momentum_bonus_value
        
    tech_data['rsi_momentum_bonus_value'] = rsi_momentum_bonus_value

    # G. 出来高/OBV確証ボーナス (5点)
    # OBVがSMAの上にあるか
    obv_momentum_bonus_value = 0.0
    if is_obv_confirming:
        obv_momentum_bonus_value = OBV_MOMENTUM_BONUS
        
    total_score += obv_momentum_bonus_value
    tech_data['obv_momentum_bonus_value'] = obv_momentum_bonus_value

    # H. 出来高スパイクボーナス (7点)
    # 現在の出来高が直近20期間の平均出来高の1.5倍以上
    volume_increase_bonus_value = 0.0
    volume_sma_20 = df['volume'].iloc[-21:-1].mean()
    if not np.isnan(volume_sma_20) and last_candle['volume'] > volume_sma_20 * 1.5:
        volume_increase_bonus_value = VOLUME_INCREASE_BONUS

    total_score += volume_increase_bonus_value
    tech_data['volume_increase_bonus_value'] = volume_increase_bonus_value
    
    # I. 流動性ボーナス (7点)
    # 出来高がTOP_SYMBOL_LIMITのトップ25%以内であれば最大ボーナス、それ以外は線形に減少
    # ここでは、簡略化のため、現在の監視リストにいること（出来高上位）を前提とする
    # 💡 出来高の絶対値ではなく、取引所から取得した出来高順位に基づいて計算すべきだが、ここでは定数ボーナスを使用
    liquidity_bonus_value = LIQUIDITY_BONUS_MAX # とりあえず、監視対象であれば常に最大ボーナスを適用
    
    total_score += liquidity_bonus_value
    tech_data['liquidity_bonus_value'] = liquidity_bonus_value
    
    # J. ボラティリティペナルティ (低ボラティリティ)
    # BBW (バンド幅) がVOLATILITY_BB_PENALTY_THRESHOLD (1%)未満の場合にペナルティ
    volatility_penalty_value = 0.0
    if last_candle['BBW'] < VOLATILITY_BB_PENALTY_THRESHOLD:
        volatility_penalty_value = -0.05 # 5点ペナルティ (低ボラティリティ相場を避ける)
        total_score += volatility_penalty_value

    tech_data['volatility_penalty_value'] = volatility_penalty_value

    # K. マクロ環境ボーナス/ペナルティ (5点)
    # FGIプロキシ + 為替ボーナス をそのままスコアに加算
    sentiment_fgi_proxy_bonus = macro_context.get('fgi_proxy', 0.0) + macro_context.get('forex_bonus', 0.0)
    # 最大ボーナス/ペナルティを FGI_PROXY_BONUS_MAX に制限
    sentiment_fgi_proxy_bonus = min(max(sentiment_fgi_proxy_bonus, -FGI_PROXY_BONUS_MAX), FGI_PROXY_BONUS_MAX)
    
    total_score += sentiment_fgi_proxy_bonus
    tech_data['sentiment_fgi_proxy_bonus'] = sentiment_fgi_proxy_bonus

    # スコアのクリッピング (0.0から1.0の間に収める)
    final_score = min(max(total_score, 0.0), 1.0)
    
    # 5. シグナルデータの組み立て
    signal = {
        'symbol': market_tickers.get(symbol, {}).get('symbol', symbol), # fetch_tickersから取得したシンボル名を使用
        'timeframe': timeframe,
        'score': final_score,
        'entry_price': entry_price,
        'stop_loss': stop_loss,
        'take_profit': take_profit,
        'rr_ratio': rr_target,
        'current_price': current_price,
        'tech_data': tech_data,
        'last_log_time': time.time()
    }
    
    return signal

# ====================================================================================
# TRADING LOGIC
# ====================================================================================

async def adjust_order_amount(symbol: str, target_usdt_amount: float, price: float) -> Tuple[float, float]:
    """
    取引所の最小/ステップサイズに基づいて注文数量を調整し、最終的なUSDTコストを返す。
    """
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        return 0.0, 0.0

    if symbol not in EXCHANGE_CLIENT.markets:
        logging.error(f"❌ シンボル {symbol} が取引所 {EXCHANGE_CLIENT.name} に存在しません。")
        return 0.0, 0.0

    market = EXCHANGE_CLIENT.markets[symbol]
    
    # 1. 注文数量の計算 (ベース通貨)
    base_amount = target_usdt_amount / price
    
    # 2. 数量の精度調整 (取引所の最小ロット/ステップサイズ)
    precision = market['precision'].get('amount')
    min_amount = market['limits'].get('amount', {}).get('min', 0.0)
    
    if precision is not None:
        # floatの精度計算 (例: precision=4 の場合、小数点以下4桁に丸める)
        # amount_to_string/amount_to_precision を使えば安全だが、ここではccxtのメソッドに依存しない方法で
        power_of_ten = 10**precision
        base_amount_rounded = math.floor(base_amount * power_of_ten) / power_of_ten
    else:
        # 精度情報がない場合はそのまま
        base_amount_rounded = base_amount

    # 3. 最小数量チェック
    if base_amount_rounded < min_amount:
        # 最小ロットを満たさない
        logging.warning(f"⚠️ {symbol} 数量調整: {base_amount_rounded:.8f} は最小ロット {min_amount} を満たしません。")
        return 0.0, 0.0

    final_usdt_amount = base_amount_rounded * price
    
    # 4. 最小USDT取引額のチェック
    min_cost = market['limits'].get('cost', {}).get('min', 0.0) # USDT取引額の最小値
    if min_cost > 0 and final_usdt_amount < min_cost:
        logging.warning(f"⚠️ {symbol} 取引額調整: {final_usdt_amount:.2f} USDT は最小取引額 {min_cost:.2f} USDT を満たしません。")
        return 0.0, 0.0
    
    return base_amount_rounded, final_usdt_amount

def calculate_dynamic_lot_size(score: float, account_status: Dict) -> float:
    """スコアと総資産に基づいて取引ロットサイズを動的に計算する"""
    global GLOBAL_TOTAL_EQUITY, DYNAMIC_LOT_MIN_PERCENT, DYNAMIC_LOT_MAX_PERCENT, DYNAMIC_LOT_SCORE_MAX
    
    # 1. 最小ロットサイズ (USDT)
    min_usdt_lot = BASE_TRADE_SIZE_USDT

    # 2. 総資産額の取得
    total_equity = account_status.get('total_equity', GLOBAL_TOTAL_EQUITY)
    
    # 総資産額が計算できていない、または少なすぎる場合は最小ロットを返す
    if total_equity < min_usdt_lot * (1 / DYNAMIC_LOT_MIN_PERCENT) or total_equity <= 0:
        return min_usdt_lot

    # 3. 動的ロットの計算範囲 (総資産のX%からY%)
    min_lot_from_equity = total_equity * DYNAMIC_LOT_MIN_PERCENT
    max_lot_from_equity = total_equity * DYNAMIC_LOT_MAX_PERCENT
    
    # ベースのロットサイズを、BASE_TRADE_SIZE_USDTとmin_lot_from_equityの大きい方に設定
    base_lot = max(min_usdt_lot, min_lot_from_equity)
    
    # 4. スコアによる重み付け (線形補間)
    # スコアが SIGNAL_THRESHOLD_NORMAL (0.84) でベースロット、DYNAMIC_LOT_SCORE_MAX (0.96) で最大ロットになるように調整
    min_score_for_dynamic = SIGNAL_THRESHOLD_NORMAL
    max_score_for_dynamic = DYNAMIC_LOT_SCORE_MAX
    
    if score >= max_score_for_dynamic:
        calculated_lot = max_lot_from_equity
    elif score <= min_score_for_dynamic:
        calculated_lot = base_lot
    else:
        # 線形補間
        range_score = max_score_for_dynamic - min_score_for_dynamic
        range_lot = max_lot_from_equity - base_lot
        
        # 0から1の間の重み (スコアがmin_score_for_dynamicの時0、max_score_for_dynamicの時1)
        weight = (score - min_score_for_dynamic) / range_score
        
        calculated_lot = base_lot + (range_lot * weight)
    
    # 5. 最終ロットサイズをクリップ (base_lotとmax_lot_from_equityの間)
    final_lot_size = min(max(calculated_lot, base_lot), max_lot_from_equity)
    
    # 6. 最小ロットサイズを下回らないことを最終チェック
    return max(final_lot_size, min_usdt_lot)

async def place_sl_tp_orders(
    symbol: str, 
    filled_amount: float, 
    stop_loss: float, 
    take_profit: float
) -> Dict:
    """ 
    現物ポジションのストップロス (SL) とテイクプロフィット (TP) 注文を同時に設定する。
    Returns: {'status': 'ok', 'sl_order_id': '...', 'tp_order_id': '...'} または 
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
            params={ # TP注文を示すカスタムパラメータがあれば追加
                'clientOrderId': f'TP-{uuid.uuid4()}'
            }
        )
        tp_order_id = tp_order['id']
        logging.info(f"✅ TP注文成功: ID={tp_order_id}, Price={format_price_precision(take_profit)}")
    except Exception as e:
        # TP設定失敗は致命的ではないが、ログに記録
        logging.error(f"❌ TP注文失敗 ({symbol}): {e}")
        # TPが設定できなくてもSLが設定できればポジションは管理可能なので、一旦続行

    # 2. SL (ストップロス) ストップリミット売り注文の設定 (Stop Limit Sell)
    try:
        # Stop-Lossは取引所ごとに設定方法が異なるため、ccxtの unified stopLoss/takeProfit メソッドがあればそちらを使用
        # 現物取引では、通常はストップリミット注文（トリガー価格とリミット価格）を使用する
        # Stop Limit Order (Example for MEXC/Bybit Spot - requires custom params)
        
        # 多くの取引所で標準の 'stop_limit' 注文を使用
        # TriggerPrice = stop_loss (またはそれに近い価格), LimitPrice = stop_loss * 0.999 (少し低めに設定)
        sl_limit_price = stop_loss * 0.995 # SL価格より少し下の指値で確実に約定させるためのバッファ
        
        sl_order = await EXCHANGE_CLIENT.create_order(
            symbol=symbol,
            type='stop_limit',
            side='sell',
            amount=filled_amount,
            price=sl_limit_price, # ストップリミットの指値価格
            params={
                'stopPrice': stop_loss, # トリガー価格
                'clientOrderId': f'SL-{uuid.uuid4()}'
            }
        )
        sl_order_id = sl_order['id']
        logging.info(f"✅ SL注文成功: ID={sl_order_id}, TriggerPrice={format_price_precision(stop_loss)}, LimitPrice={format_price_precision(sl_limit_price)}")
        
    except Exception as e:
        logging.error(f"❌ SL注文失敗 ({symbol}): {e}")
        # SL/TP注文が両方とも失敗した場合、またはSL注文が失敗した場合、ポジションは管理できない
        
        # 💡 TP注文が成功していた場合、そのTP注文をキャンセルする必要がある
        if tp_order_id:
            try:
                await EXCHANGE_CLIENT.cancel_order(tp_order_id, symbol)
                logging.warning(f"⚠️ SL注文失敗に伴い、TP注文 (ID: {tp_order_id}) をキャンセルしました。")
            except Exception as cancel_e:
                logging.error(f"❌ TP注文のキャンセルにも失敗 ({symbol}, ID: {tp_order_id}): {cancel_e}")
                
        return {'status': 'error', 'error_message': f'SL注文失敗: {e}'}

    # SLまたはTPが少なくとも一つ設定できた場合（ここでは両方設定成功が前提）
    # TP注文のみが成功し、SL注文が失敗した場合でも、SL注文失敗でリターンされているため、この時点では両方成功しているか、TPはキャンセル済み。
    if sl_order_id:
         return {'status': 'ok', 'sl_order_id': sl_order_id, 'tp_order_id': tp_order_id}
    else:
         # SL設定失敗で既にエラーリターンされているはずだが、念のため
         return {'status': 'error', 'error_message': 'SL/TP注文が設定できませんでした。'}


async def close_position_immediately(symbol: str, amount: float) -> Dict:
    """
    不完全なポジションを強制的に成行売りでクローズする。
    Args:
        symbol (str): シンボル
        amount (float): クローズする数量
    Returns:
        Dict: クローズ結果 {'status': 'ok'/'error'/'skipped', 'closed_amount': float, 'error_message': str}
    """
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY or amount <= 0:
        return {'status': 'skipped', 'error_message': 'スキップ: クライアント未準備または数量がゼロです。'}

    logging.warning(f"🚨 不完全ポジションの強制クローズを試みます: {symbol} (数量: {amount:.4f})")
    
    try:
        # 数量の丸め（成行注文でも精度は重要）
        # 💡 ccxt.create_orderのamountは、ベース通貨の数量を渡す。
        # amount * 1.01 * EXCHANGE_CLIENT.markets[symbol]['info']['price'] はロットサイズ計算のロジックとしては不正確
        # ここでは、渡された amount (ベース通貨の数量) を直接使用し、取引所の精度で丸める
        base_amount_rounded, _ = await adjust_order_amount(
            symbol=symbol, 
            target_usdt_amount=amount * EXCHANGE_CLIENT.markets[symbol]['info']['price'], # 概算USDT価値
            price=EXCHANGE_CLIENT.markets[symbol]['info']['price'] # 現在の価格
        ) 
        # adjust_order_amount は数量 (base_amount) を返すため、その値を再利用
        amount = base_amount_rounded 

        # 成行売り注文
        close_order = await EXCHANGE_CLIENT.create_order(
            symbol=symbol,
            type='market',
            side='sell',
            amount=amount
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
    
    if TEST_MODE or not API_KEY or not SECRET_KEY:
        # テストモードまたはAPIキーがない場合は、シミュレーション結果を返す
        return {
            'status': 'ok',
            'filled_amount': signal['lot_size_usdt'] / signal['entry_price'], # 想定数量
            'filled_usdt': signal['lot_size_usdt'],
            'sl_order_id': 'TEST-SL-12345',
            'tp_order_id': 'TEST-TP-67890',
            'entry_price': signal['entry_price']
        }

    symbol = signal['symbol']
    entry_price = signal['entry_price']
    
    # 1. 動的ロットサイズの計算
    lot_size_usdt = calculate_dynamic_lot_size(signal['score'], account_status)
    signal['lot_size_usdt'] = lot_size_usdt # シグナルデータにロットサイズを保存

    # 2. 注文数量の調整
    # 注文価格: entry_price (シグナルで決定した指値価格)
    base_amount_to_buy, final_usdt_amount = await adjust_order_amount(symbol, lot_size_usdt, entry_price)

    if base_amount_to_buy <= 0.0:
        return {'status': 'error', 'error_message': '調整後の数量が取引所の最小要件を満たしません。', 'close_status': 'skipped'}

    logging.info(f"⏳ 注文実行: {symbol} @ {format_price_precision(entry_price)}. 数量: {base_amount_to_buy:.4f} ({format_usdt(final_usdt_amount)} USDT)")

    # 3. 現物指値買い注文 (IOC注文: 即時約定しなかった場合はキャンセル)
    buy_order = None
    try:
        # IOC (Immediate Or Cancel) 注文
        buy_order = await EXCHANGE_CLIENT.create_order(
            symbol=symbol,
            type='limit', # 指値
            side='buy',
            amount=base_amount_to_buy,
            price=entry_price,
            params={
                'timeInForce': 'IOC' # 即時約定しなければキャンセル
            }
        )
        
        # 4. 約定結果の確認
        filled_amount = buy_order.get('filled', 0.0)
        filled_usdt = buy_order.get('cost', 0.0) # 約定コスト
        
        if filled_amount <= 0.0:
            # IOC注文で即時約定しなかったため、注文は自動的にキャンセルされている
            logging.warning(f"❌ {symbol}: IOC指値買い注文が即時約定しなかったため、注文はキャンセルされました。")
            return {'status': 'error', 'error_message': '指値買い注文が即時約定しなかったため、キャンセルされました。', 'close_status': 'skipped'}

        # 5. SL/TP注文の設定
        sl_tp_result = await place_sl_tp_orders(
            symbol=symbol,
            filled_amount=filled_amount,
            stop_loss=signal['stop_loss'],
            take_profit=signal['take_profit']
        )
        
        if sl_tp_result['status'] == 'ok':
            # 6. ポジションを管理リストに追加
            OPEN_POSITIONS.append({
                'id': buy_order['id'], # 注文IDをポジションIDとして使用
                'symbol': symbol,
                'entry_price': entry_price,
                'filled_amount': filled_amount,
                'filled_usdt': filled_usdt,
                'stop_loss': signal['stop_loss'],
                'take_profit': signal['take_profit'],
                'sl_order_id': sl_tp_result['sl_order_id'],
                'tp_order_id': sl_tp_result['tp_order_id'],
                'timestamp': time.time()
            })
            logging.info(f"✅ {symbol}: 取引成功。ポジションを管理リストに追加。")

            return {
                'status': 'ok',
                'filled_amount': filled_amount,
                'filled_usdt': filled_usdt,
                'sl_order_id': sl_tp_result['sl_order_id'],
                'tp_order_id': sl_tp_result['tp_order_id'],
                'entry_price': entry_price
            }
        else:
            # 7. SL/TP設定失敗: 不完全なポジションを即時クローズする
            logging.error(f"❌ {symbol}: SL/TP注文設定失敗。不完全ポジションを強制クローズします。")
            
            # クローズ処理を実行
            close_result = await close_position_immediately(symbol, filled_amount)
            
            return {
                'status': 'error',
                'error_message': sl_tp_result['error_message'],
                'close_status': close_result['status'],
                'closed_amount': close_result.get('closed_amount', 0.0),
                'close_error_message': close_result.get('error_message'),
            }

    except ccxt.InsufficientFunds as e:
        error_message = f"残高不足エラー: {e}"
        logging.error(f"❌ 取引実行失敗 ({symbol}): {error_message}", exc_info=True)
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

# ====================================================================================
# POSITION MANAGEMENT LOGIC
# ====================================================================================

async def check_order_status(order_id: str, symbol: str) -> Optional[Dict]:
    """注文IDとシンボルに基づいて注文ステータスを取得する"""
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        return None
        
    try:
        order = await EXCHANGE_CLIENT.fetch_order(order_id, symbol)
        return order
    except ccxt.OrderNotFound:
        # 注文が見つからない場合は、完全に約定したか、キャンセルされたと見なす
        return {'status': 'closed'}
    except Exception as e:
        logging.error(f"❌ 注文ステータス取得中にエラーが発生: {symbol}, ID={order_id}, Error: {e}")
        return None

async def open_order_management_loop():
    """オープン注文（ポジション）の状態を監視し、決済されたものを処理する"""
    global OPEN_POSITIONS, GLOBAL_TOTAL_EQUITY
    
    if not OPEN_POSITIONS:
        return
        
    logging.debug(f"ℹ️ オープン注文監視ループ開始。監視中のポジション: {len(OPEN_POSITIONS)}")
    
    positions_to_remove_ids = []
    
    for position in OPEN_POSITIONS:
        symbol = position['symbol']
        sl_order_id = position['sl_order_id']
        tp_order_id = position['tp_order_id']
        
        is_closed = False
        closed_result = None
        exit_type = None
        
        # 1. SL/TP注文のステータスチェック
        sl_status = await check_order_status(sl_order_id, symbol)
        tp_status = await check_order_status(tp_order_id, symbol)
        
        sl_open = sl_status and sl_status.get('status') in ['open', 'limit'] # Stop Limit order might be 'open'
        tp_open = tp_status and tp_status.get('status') in ['open', 'limit']
        
        # 2. 決済ロジックの判定
        if sl_status and sl_status.get('status') == 'closed' and sl_status.get('filled', 0.0) > 0.0:
            # SL注文が約定した場合
            logging.info(f"🛑 {symbol}: SL注文 (ID: {sl_order_id}) が約定しました。")
            exit_type = 'SL約定'
            is_closed = True
            # 残りのTP注文をキャンセル
            if tp_open:
                try:
                    await EXCHANGE_CLIENT.cancel_order(tp_order_id, symbol)
                    logging.info(f"✅ TP注文 (ID: {tp_order_id}) をキャンセルしました。")
                except Exception as e:
                    logging.error(f"❌ SL約定後のTP注文キャンセル失敗 ({tp_order_id}): {e}")
                    
        elif tp_status and tp_status.get('status') == 'closed' and tp_status.get('filled', 0.0) > 0.0:
            # TP注文が約定した場合
            logging.info(f"🛑 {symbol}: TP注文 (ID: {tp_order_id}) が約定しました。")
            exit_type = 'TP約定'
            is_closed = True
            # 残りのSL注文をキャンセル
            if sl_open:
                try:
                    await EXCHANGE_CLIENT.cancel_order(sl_order_id, symbol)
                    logging.info(f"✅ SL注文 (ID: {sl_order_id}) をキャンセルしました。")
                except Exception as e:
                    logging.error(f"❌ TP約定後のSL注文キャンセル失敗 ({sl_order_id}): {e}")
                    
        elif not sl_status and not tp_status:
             # 両方の注文ステータスが取得できない場合 (APIエラーなど)
             logging.warning(f"⚠️ {symbol}: SL/TP注文ステータスが両方取得できません。手動決済された可能性があります。")
             is_closed = True
             exit_type = '手動決済?'
             
        elif not sl_open or not tp_open:
            # 💡 V19.0.53 【SL/TP再設定ロジック】: どちらかの注文が欠けている場合 (ユーザーの手動操作や取引所の自動キャンセルを検知)
            
            # 手動決済ではない (両方ともclosed/filledでない) かつ、片方または両方が欠けている（未設定状態）
            
            # 1. 注文IDが有効だがclosed/canceledになっている（取引所側で何らかの理由でキャンセルされた）
            # 2. 注文IDが無効（初期設定時に失敗した状態で、ポジションが管理リストに残っている）
            
            # 💡 どちらか一方の注文がclosedになっていない（つまり、約定したと見なせない）のに、openでもない場合
            if sl_status and sl_status.get('status') in ['closed', 'canceled', 'rejected'] and sl_status.get('filled', 0.0) == 0.0 and tp_open:
                 logging.warning(f"⚠️ {symbol}: SL注文 (ID: {sl_order_id}) が約定なしでクローズ/キャンセルされました。TP注文をキャンセルし、SL/TPを再設定します。")
                 # TPをキャンセル
                 try:
                     await EXCHANGE_CLIENT.cancel_order(tp_order_id, symbol)
                 except Exception as e:
                     logging.error(f"❌ SL欠落後のTPキャンセル失敗 ({tp_order_id}): {e}")
                     continue # 次のポジションへ
                     
                 # SL/TPを再設定
                 sl_tp_result = await place_sl_tp_orders(
                    symbol=symbol,
                    filled_amount=position['filled_amount'],
                    stop_loss=position['stop_loss'],
                    take_profit=position['take_profit']
                 )
                 if sl_tp_result['status'] == 'ok':
                     logging.info(f"✅ {symbol}: SL/TP注文を再設定しました。")
                     # ポジション情報を更新
                     position['sl_order_id'] = sl_tp_result['sl_order_id']
                     position['tp_order_id'] = sl_tp_result['tp_order_id']
                 else:
                     logging.error(f"❌ {symbol}: SL/TPの再設定に失敗しました。このポジションは管理対象から外します。")
                     is_closed = True # 強制的にクローズ扱いにして管理対象から外す
                     exit_type = '再設定失敗'
                     
            elif tp_status and tp_status.get('status') in ['closed', 'canceled', 'rejected'] and tp_status.get('filled', 0.0) == 0.0 and sl_open:
                 logging.warning(f"⚠️ {symbol}: TP注文 (ID: {tp_order_id}) が約定なしでクローズ/キャンセルされました。SL注文をキャンセルし、SL/TPを再設定します。")
                 # SLをキャンセル
                 try:
                     await EXCHANGE_CLIENT.cancel_order(sl_order_id, symbol)
                 except Exception as e:
                     logging.error(f"❌ TP欠落後のSLキャンセル失敗 ({sl_order_id}): {e}")
                     continue # 次のポジションへ
                     
                 # SL/TPを再設定
                 sl_tp_result = await place_sl_tp_orders(
                    symbol=symbol,
                    filled_amount=position['filled_amount'],
                    stop_loss=position['stop_loss'],
                    take_profit=position['take_profit']
                 )
                 if sl_tp_result['status'] == 'ok':
                     logging.info(f"✅ {symbol}: SL/TP注文を再設定しました。")
                     # ポジション情報を更新
                     position['sl_order_id'] = sl_tp_result['sl_order_id']
                     position['tp_order_id'] = sl_tp_result['tp_order_id']
                 else:
                     logging.error(f"❌ {symbol}: SL/TPの再設定に失敗しました。このポジションは管理対象から外します。")
                     is_closed = True # 強制的にクローズ扱いにして管理対象から外す
                     exit_type = '再設定失敗'
                     
            elif sl_open or tp_open:
                # どちらか一方がオープン中である場合は、引き続きオープン中として処理を継続
                logging.debug(f"ℹ️ {symbol}: ポジションは引き続きオープン中 (SL: {sl_open}, TP: {tp_open})")
                pass

        if is_closed:
            positions_to_remove_ids.append(position['id'])
            
            # 約定価格は履歴から取得が必要だが、ここでは簡略化のため0.0とする
            # PnLの計算は、最新の市場価格を使用して行う（または取引所APIから最終約定価格を取得）
            try:
                # 最終的なPnL計算のため、最新価格を取得
                ticker = await EXCHANGE_CLIENT.fetch_ticker(symbol)
                last_price = ticker['last']
                
                # 簡略化したPnL計算
                entry_price = position['entry_price']
                exit_price = last_price # 暫定的に最新価格を決済価格とする

                # 決済価格を、SL/TPの約定価格 (sl_status/tp_status の average) から取得できればより正確
                if exit_type == 'SL約定' and sl_status and sl_status.get('average'):
                    exit_price = sl_status['average']
                elif exit_type == 'TP約定' and tp_status and tp_status.get('average'):
                    exit_price = tp_status['average']
                # ユーザー手動決済の場合は、最新価格を使用するしかない
                
                pnl_usdt = (exit_price - entry_price) * position['filled_amount']
                pnl_percent = (pnl_usdt / position['filled_usdt']) * 100 if position['filled_usdt'] > 0 else 0.0

                # 総資産額の再取得
                account_status_after_close = await fetch_account_status()
                current_total_equity = account_status_after_close.get('total_equity', GLOBAL_TOTAL_EQUITY)
                GLOBAL_TOTAL_EQUITY = current_total_equity # グローバル更新
                
                closed_result = {
                    'symbol': position['symbol'],
                    'entry_price': position['entry_price'],
                    'exit_price': exit_price,
                    'pnl_usdt': pnl_usdt,
                    'pnl_percent': pnl_percent,
                    'exit_type': exit_type,
                    'status': 'ok',
                    'total_equity_after': current_total_equity
                }
                
                # 決済通知
                # 決済時には timeframe 情報がないため、'1h'を仮で設定
                signal_for_log = {'symbol': symbol, 'timeframe': '1h'}
                notification_message = format_telegram_message(signal_for_log, "ポジション決済", SIGNAL_THRESHOLD_NORMAL, closed_result)
                await send_telegram_notification(notification_message)
                log_signal(closed_result, "ポジション決済")

            except Exception as e:
                logging.error(f"❌ 決済処理後のPnL計算/通知中にエラーが発生 ({symbol}): {e}")

    # 決済完了したポジションをリストから削除
    OPEN_POSITIONS = [p for p in OPEN_POSITIONS if p['id'] not in positions_to_remove_ids]
    logging.debug(f"✅ オープン注文監視ループ完了。現在の監視ポジション数: {len(OPEN_POSITIONS)}")

# ====================================================================================
# MAIN BOT LOGIC
# ====================================================================================

async def analyze_symbol(symbol: str, market_tickers: Dict[str, Dict]) -> List[Dict]:
    """特定のシンボルに対して複数のタイムフレームで分析を実行する"""
    global REQUIRED_OHLCV_LIMITS, HOURLY_ATTEMPT_LOG
    
    tasks = []
    
    for tf in TARGET_TIMEFRAMES:
        required_limit = REQUIRED_OHLCV_LIMITS.get(tf, 500)
        # 各タイムフレームの分析タスクを作成
        tasks.append(
            fetch_ohlcv_and_analyze(symbol, tf, required_limit, market_tickers)
        )
        
    # 全てのタイムフレームでの分析を並行して実行
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    signals = []
    has_error = False
    
    for result in results:
        if isinstance(result, Exception):
            logging.error(f"❌ {symbol} の分析中にエラー: {result}")
            has_error = True
        elif result and result['score'] >= BASE_SCORE:
            # ベーススコア (0.50) 以上の有効なシグナルのみを収集
            signals.append(result)

    # エラーがなく、シグナルも見つからなかった場合、ログに記録
    if not has_error and not signals:
         HOURLY_ATTEMPT_LOG[symbol] = "スコア低迷" # 0.50未満

    return signals


async def main_bot_loop():
    """ボットのメイン実行ループ (1分ごと)"""
    global LAST_SUCCESS_TIME, LAST_SIGNAL_TIME, LAST_ANALYSIS_SIGNALS, CURRENT_MONITOR_SYMBOLS, GLOBAL_MACRO_CONTEXT, LAST_HOURLY_NOTIFICATION_TIME, IS_FIRST_MAIN_LOOP_COMPLETED, HOURLY_SIGNAL_LOG, HOURLY_ATTEMPT_LOG, BOT_VERSION
    
    start_time = time.time()
    now_jst = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")
    logging.info(f"--- 💡 {now_jst} - BOT LOOP START (M1 Frequency) ---")

    # 1. FGIデータを取得し、グローバルマクロコンテキストを更新
    GLOBAL_MACRO_CONTEXT = await fetch_fgi_data() 
    # FGIの値をスコアリングに反映する準備
    # macro_influence_score = (GLOBAL_MACRO_CONTEXT.get('fgi_proxy', 0.0) + GLOBAL_MACRO_CONTEXT.get('forex_bonus', 0.0))
    current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)

    # 2. 口座ステータスを取得し、新規取引の可否をチェック
    account_status = await fetch_account_status()
    if account_status.get('error'):
        logging.critical("🚨 口座ステータスの取得に失敗しました。取引をスキップします。")
        # 初回起動完了通知をスキップさせないため、一旦処理を続行させる

    # 3. 出来高上位銘柄を更新 (1時間ごと)
    if time.time() - LAST_SUCCESS_TIME > 60 * 60:
        CURRENT_MONITOR_SYMBOLS = await fetch_top_symbols()
        LAST_SUCCESS_TIME = time.time()
        
    # 4. 全ての監視銘柄の最新ティッカーを取得 (並行処理の効率化のため)
    market_tickers = {}
    try:
        tickers = await EXCHANGE_CLIENT.fetch_tickers(symbols=CURRENT_MONITOR_SYMBOLS)
        market_tickers = {k: v for k, v in tickers.items() if v}
    except Exception as e:
        logging.error(f"❌ ティッカー情報の取得に失敗: {e}. 現在の監視銘柄リストをリセットします。")
        market_tickers = {} # ティッカー情報取得失敗は無視して、分析フェーズでエラーを処理させる

    # 5. 全ての監視銘柄の分析を並行して実行
    analysis_tasks = [
        analyze_symbol(symbol, market_tickers)
        for symbol in CURRENT_MONITOR_SYMBOLS
        if symbol in market_tickers # ティッカー情報がある銘柄のみ分析対象とする
    ]
    
    all_results_list = await asyncio.gather(*analysis_tasks)
    
    # 6. 結果を統合し、スコアの高い順にソート
    all_signals = [signal for sublist in all_results_list for signal in sublist]
    
    # 💡 Hourly Report用のログをクリアして格納
    HOURLY_SIGNAL_LOG = sorted(all_signals, key=lambda x: x['score'], reverse=True)
    
    # 7. クールダウンと閾値でフィルタリング
    tradable_signals = []
    for signal in HOURLY_SIGNAL_LOG:
        symbol = signal['symbol']
        score = signal['score']
        
        # 閾値チェック
        if score < current_threshold:
            continue
            
        # クールダウンチェック (同一銘柄の取引は2時間空ける)
        if time.time() - LAST_SIGNAL_TIME.get(symbol, 0) < TRADE_SIGNAL_COOLDOWN:
            continue
            
        # 既にポジションがある銘柄はスキップ
        if any(p['symbol'] == symbol for p in OPEN_POSITIONS):
            continue
            
        tradable_signals.append(signal)

    # 8. ベストシグナルを取得
    best_signal = tradable_signals[0] if tradable_signals else None

    # 9. ポジション決済と管理ループ (メイン分析とは非同期で実行されているはずだが、ここでも実行)
    # open_order_management_loop() # これは別タスクで実行されるため、メインループではスキップ

    # 10. 取引実行
    trade_result = None
    if best_signal:
        logging.info(f"💡 最適シグナルが見つかりました: {best_signal['symbol']} (Score: {best_signal['score']*100:.2f})")
        
        # 新規取引の可否チェック (テストモードではない & APIキーが設定されている)
        if not TEST_MODE and API_KEY and SECRET_KEY:
             # USDT残高チェック
            if account_status['total_usdt_balance'] >= MIN_USDT_BALANCE_FOR_TRADE:
                # 取引実行
                trade_result = await execute_trade(best_signal, account_status)
                
                if trade_result['status'] == 'ok':
                    LAST_SIGNAL_TIME[best_signal['symbol']] = time.time() # クールダウンタイマーをセット
                # 失敗時もログは execute_trade 内で記録される
            else:
                error_message = f"残高不足 (現在: {format_usdt(account_status['total_usdt_balance'])} USDT)。新規取引に必要な額: {MIN_USDT_BALANCE_FOR_TRADE:.2f} USDT。"
                trade_result = {'status': 'error', 'error_message': error_message, 'close_status': 'skipped'}
                logging.warning(f"⚠️ {best_signal['symbol']} 取引スキップ: {error_message}")
        else:
            logging.info(f"ℹ️ {best_signal['symbol']} は閾値 {current_threshold*100:.2f} を満たしていません。取引をスキップします。")
            # 閾値を満たさないシグナルもログには記録する
            # テストモードの実行シミュレーション (取引はしないが、通知とログは行う)
            if TEST_MODE and best_signal['score'] >= current_threshold:
                 # テストモードで取引実行シミュレーション
                 trade_result = await execute_trade(best_signal, account_status) # trade_resultはシミュレーション結果を返す

    # 11. Telegram通知
    if best_signal:
        if (trade_result and trade_result.get('status') == 'ok') or (TEST_MODE and trade_result and trade_result.get('status') == 'ok'): # TEST_MODEで成功シミュレーションの場合も通知
            # 取引成功またはテストモードの場合は通知
            notification_message = format_telegram_message(best_signal, "取引シグナル", current_threshold, trade_result)
            await send_telegram_notification(notification_message)
            log_signal(best_signal, "取引シグナル (成功/テスト)")
        elif trade_result and trade_result.get('status') == 'error':
            # 取引失敗の場合は通知 (エラー詳細を含む)
            notification_message = format_telegram_message(best_signal, "取引シグナル", current_threshold, trade_result)
            await send_telegram_notification(notification_message)
            log_signal(best_signal, "取引シグナル (失敗)")
        # 閾値未満でスキップされた場合は通知しない

    # 12. 初回起動完了通知 (一度だけ)
    if not IS_FIRST_MAIN_LOOP_COMPLETED:
        # account_statusは再取得せず、ループ開始時のものを使用
        startup_message = format_startup_message(account_status, GLOBAL_MACRO_CONTEXT, len(CURRENT_MONITOR_SYMBOLS), current_threshold, BOT_VERSION)
        await send_telegram_notification(startup_message)
        IS_FIRST_MAIN_LOOP_COMPLETED = True

    # 13. Hourly Reportの送信 (1時間ごと)
    if time.time() - LAST_HOURLY_NOTIFICATION_TIME > HOURLY_SCORE_REPORT_INTERVAL:
        hourly_report_message = format_hourly_report(HOURLY_SIGNAL_LOG, HOURLY_ATTEMPT_LOG, LAST_HOURLY_NOTIFICATION_TIME, current_threshold, BOT_VERSION)
        await send_telegram_notification(hourly_report_message)
        LAST_HOURLY_NOTIFICATION_TIME = time.time()
        # ログをクリア
        HOURLY_ATTEMPT_LOG = {}
        
    logging.info("--- ✅ BOT LOOP END ---")


# ====================================================================================
# ASYNCIO SETUP & WEB SERVER
# ====================================================================================

# 1. CCXTクライアントの初期化 (非同期ループの外で実行する必要があるため)
# 💡 uvicornの起動前にinitialize_exchange_clientが呼ばれるように調整が必要
initialize_exchange_client()
# uvicornからFastAPIが起動されるため、メインの実行ロジックを非同期タスクとして登録する

app = FastAPI(title="Apex BOT API", version=BOT_VERSION)

async def start_background_tasks():
    """メインBOTループとポジション管理ループをバックグラウンドで起動する"""
    
    # 💡 open_order_management_loop はメインループよりも短い間隔で実行する
    # 独立したタスクとして実行
    app.state.open_order_task = asyncio.create_task(
        run_periodic_task(open_order_management_loop, MONITOR_INTERVAL)
    )
    
    # メインBOTループ
    app.state.main_bot_task = asyncio.create_task(
        run_periodic_task(main_bot_loop, LOOP_INTERVAL)
    )
    logging.info("✅ バックグラウンドタスク (メインループ & 注文管理) を起動しました。")


async def run_periodic_task(task_func: Callable[[], Any], interval: int):
    """指定された間隔で非同期タスクを繰り返し実行する"""
    # 初回実行前にクライアントが準備できるまで待機 (最大60秒)
    for _ in range(60):
        if IS_CLIENT_READY:
            break
        await asyncio.sleep(1)
    
    # 待機しても準備が完了しない場合は警告を出すが、タスクは実行を試みる
    if not IS_CLIENT_READY:
         logging.critical(f"🚨 {task_func.__name__} の実行開始: CCXTクライアントが初期化されていませんが、タスクを実行します。")


    # 初回起動時は即時実行
    logging.info(f"⏳ {task_func.__name__}: 初回実行を開始します。")
    try:
        await task_func()
    except Exception as e:
        logging.critical(f"❌ {task_func.__name__}: 初回実行中に致命的なエラーが発生: {e}", exc_info=True)
        # 致命的なエラーが発生した場合でも、ループは続行させる

    while True:
        try:
            await asyncio.sleep(interval)
            await task_func()
        except asyncio.CancelledError:
            logging.info(f"✅ {task_func.__name__}: タスクがキャンセルされました。")
            break
        except Exception as e:
            logging.critical(f"❌ {task_func.__name__}: 実行中に致命的なエラーが発生: {e}", exc_info=True)
            
            # 致命的エラー発生時の通知
            error_message = f"🚨 **致命的なエラー発生**\n\nメインBOTループの実行中にエラーが発生しました。\n\n**エラー:** <code>{str(e)[:500]}...</code>\n\n**BOTバージョン**: <code>{BOT_VERSION}</code>"
            try:
                 await send_telegram_notification(error_message)
                 logging.info(f"✅ 致命的エラー通知を送信しました (Ver: {BOT_VERSION})")
            except Exception as notify_e:
                 logging.error(f"❌ 致命的エラー通知の送信に失敗: {notify_e}")

        # 次のループまで待機
        # await asyncio.sleep(LOOP_INTERVAL) # run_periodic_taskが担当するため、不要


@app.on_event("startup")
async def startup_event():
    """FastAPI起動時にバックグラウンドタスクを開始する"""
    # CCXTクライアントの初期化は既にグローバルスコープで実行済み
    await start_background_tasks()

@app.on_event("shutdown")
async def shutdown_event():
    """FastAPI終了時にタスクをキャンセルし、CCXTクライアントを閉じる"""
    if hasattr(app.state, 'main_bot_task') and app.state.main_bot_task:
        app.state.main_bot_task.cancel()
        await asyncio.gather(app.state.main_bot_task, return_exceptions=True)
    if hasattr(app.state, 'open_order_task') and app.state.open_order_task:
        app.state.open_order_task.cancel()
        await asyncio.gather(app.state.open_order_task, return_exceptions=True)

    if EXCHANGE_CLIENT:
        await EXCHANGE_CLIENT.close()
        logging.info("✅ CCXTクライアントをクローズしました。")


@app.get("/")
def read_root():
    """ルートエンドポイント"""
    return {"message": f"Apex BOT is running. Version: {BOT_VERSION}"}

@app.get("/status")
async def get_status():
    """現在のボットの状態を返す"""
    
    current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
    
    status_data = {
        "version": BOT_VERSION,
        "is_client_ready": IS_CLIENT_READY,
        "test_mode": TEST_MODE,
        "loop_interval_sec": LOOP_INTERVAL,
        "total_equity_usdt": GLOBAL_TOTAL_EQUITY,
        "macro_context": GLOBAL_MACRO_CONTEXT,
        "current_signal_threshold": current_threshold,
        "monitoring_symbols_count": len(CURRENT_MONITOR_SYMBOLS),
        "open_positions_count": len(OPEN_POSITIONS),
        "last_hourly_report_time": datetime.fromtimestamp(LAST_HOURLY_NOTIFICATION_TIME, JST).strftime("%Y/%m/%d %H:%M:%S") if LAST_HOURLY_NOTIFICATION_TIME > 0 else "N/A",
        "last_main_loop_time": datetime.fromtimestamp(LAST_SUCCESS_TIME, JST).strftime("%Y/%m/%d %H:%M:%S") if LAST_SUCCESS_TIME > 0 else "N/A",
        "hourly_signals_count": len(HOURLY_SIGNAL_LOG),
        "global_excluded_symbols_count": len(GLOBAL_EXCLUDED_SYMBOLS),
    }

    # 監視中のポジション詳細
    open_positions_details = []
    for pos in OPEN_POSITIONS:
        open_positions_details.append({
            'symbol': pos['symbol'],
            'entry_price': format_price_precision(pos['entry_price']),
            'filled_usdt': format_usdt(pos['filled_usdt']),
            'sl_tp': f"{format_price_precision(pos['stop_loss'])} / {format_price_precision(pos['take_profit'])}"
        })
    status_data['open_positions_details'] = open_positions_details

    return JSONResponse(content=status_data)

# ====================================================================================
# ENTRY POINT
# ====================================================================================

# uvicorn main_render:app --host 0.0.0.0 --port $PORT で実行されることを想定
# if __name__ == "__main__":
#     uvicorn.run("main_render:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), reload=True)
