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
    start_jst = datetime.fromtimestamp(start_time, JST).strftime("%H/%M/%S")
    
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
        f"  - **有効シグナル (>= 0.80)**: <code>{len([s for s in signals if s['score'] >= 0.80])}</code> 件\n"
        f"  - **現在の取引閾値**: <code>{current_threshold*100:.2f} / 100</code>\n"
        f"  - **保有中ポジション**: <code>{len(OPEN_POSITIONS)}</code> 銘柄\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
    )
    
    # ベストスコア
    if signals_sorted:
        best_signal = signals_sorted[0]
        message += (
            f"\n"
            f"🟢 **ベストスコア銘柄 (High)**\n"
            f"  - **銘柄**: <b>{best_signal['symbol']}</b> ({best_signal['timeframe']})\n"
            f"  - **スコア**: <code>{best_signal['score'] * 100:.2f} / 100</code>\n"
            f"  - **推定勝率**: <code>{get_estimated_win_rate(best_signal['score'])}</code>\n"
            f"  - **エントリー (Entry)**: <code>{format_price_precision(best_signal['entry_price'])}</code>\n"
            f"  - **SL/TP**: <code>{format_price_precision(best_signal['stop_loss'])}</code> / <code>{format_price_precision(best_signal['take_profit'])}</code>\n"
            f"\n"
        )
    else:
        message += f"\n➖ **ベストスコア銘柄は検出されませんでした**\n"
    
    # ワーストスコア
    if signals_sorted and len(signals_sorted) > 1:
        worst_signal = signals_sorted[-1]
        message += (
            f"\n"
            f"🔴 **ワーストスコア銘柄 (Low)**\n"
            f"  - **銘柄**: <b>{worst_signal['symbol']}</b> ({worst_signal['timeframe']})\n"
            f"  - **スコア**: <code>{worst_signal['score'] * 100:.2f} / 100</code>\n"
            f"  - **推定勝率**: <code>{get_estimated_win_rate(worst_signal['score'])}</code>\n"
            f"  - **エントリー (Entry)**: <code>{format_price_precision(worst_signal['entry_price'])}</code>\n"
            f"  - **SL/TP**: <code>{format_price_precision(worst_signal['stop_loss'])}</code> / <code>{format_price_precision(worst_signal['take_profit'])}</code>\n"
            f"\n"
        )
    else:
        message += f"\n➖ **ワーストスコア銘柄は検出されませんでした**\n\n"

    # スキップ理由
    if skipped_count > 0:
        skip_summary = {}
        for reason in attempt_log.values():
            skip_summary[reason] = skip_summary.get(reason, 0) + 1
        
        skip_lines = [f"    - {reason}: <code>{count}</code> 件" for reason, count in skip_summary.items()]
        
        message += (
            f"ℹ️ **分析スキップ理由 ({skipped_count} 件)**\n"
            f"{'\n'.join(skip_lines)}\n"
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
        'filled_amount': signal.get('filled_amount'),
        'filled_usdt': signal.get('filled_usdt'),
        'pnl_usdt': signal.get('pnl_usdt'),
        'pnl_percent': signal.get('pnl_percent'),
        'exit_type': signal.get('exit_type'),
        'is_test_mode': TEST_MODE
    }
    # numpy/pandasオブジェクトを標準型に変換
    log_data_compatible = _to_json_compatible(log_data)
    
    # JSON文字列としてINFOログに出力
    log_json = json.dumps(log_data_compatible, ensure_ascii=False)
    # logging.info(f"JSON_LOG: {log_json}") # デバッグ時には有効化

async def send_telegram_message(message: str):
    """Telegramにメッセージを送信する"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.warning("⚠️ TelegramのトークンまたはチャットIDが設定されていません。通知をスキップします。")
        return
        
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        'chat_id': TELEGRAM_CHAT_ID,
        'text': message,
        'parse_mode': 'HTML' # MarkdownではなくHTML形式を使用
    }
    
    try:
        # requestsはブロッキングなので、別スレッドで実行する必要がある
        await asyncio.to_thread(requests.post, url, data=payload, timeout=5)
        # logging.debug("✅ Telegram通知を送信しました。")
    except Exception as e:
        logging.error(f"❌ Telegram通知の送信中にエラーが発生: {e}")

async def fetch_account_status() -> Dict:
    """口座ステータス (USDT残高、総資産額) を取得する"""
    global EXCHANGE_CLIENT, IS_CLIENT_READY, GLOBAL_TOTAL_EQUITY, MIN_USDT_BALANCE_FOR_TRADE
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.critical("🚨 口座ステータスの取得に失敗しました。クライアントが準備できていません。")
        return {'total_usdt_balance': 0.0, 'total_equity': 0.0, 'open_positions': [], 'error': True}
        
    total_equity = 0.0
    total_usdt_balance = 0.0
    open_ccxt_positions = [] # ボットが管理していない保有資産のリスト
    
    try:
        # 1. 口座残高の取得
        balance = await EXCHANGE_CLIENT.fetch_balance()
        
        # 2. 利用可能なUSDT残高 (取引に使用可能な残高)
        total_usdt_balance = balance.get('free', {}).get('USDT', 0.0)
        
        # 3. 総資産額 (Equity) の計算
        # USDT残高をまずEquityに加算
        total_equity += balance.get('total', {}).get('USDT', 0.0)
        
        # その他の保有資産（BTC, ETHなど）の評価額をUSDT建てで加算
        tickers = await EXCHANGE_CLIENT.fetch_tickers()
        
        for currency, amount in balance.get('total', {}).items():
            if currency not in ['USDT', 'USD'] and amount is not None and amount > 0.0001:
                # 現物取引所の場合、シンボル形式は CURRENCY/USDT
                symbol = f"{currency}/USDT"
                
                # 保有資産のUSDT価値を計算
                usdt_value = 0.0
                if symbol in tickers and tickers[symbol]['last'] is not None:
                    current_price = tickers[symbol]['last']
                    usdt_value = amount * current_price
                    
                    if usdt_value >= 1.0: # 1.0 USDT以上の価値があるもののみ考慮
                        total_equity += usdt_value
                        
                        # ボットが管理していないポジションとして記録
                        if not any(p['symbol'] == symbol for p in OPEN_POSITIONS):
                            open_ccxt_positions.append({
                                'symbol': symbol,
                                'amount': amount,
                                'usdt_value': usdt_value,
                                'current_price': current_price
                            })
                            
                elif amount >= 1.0:
                    logging.warning(f"⚠️ {symbol} の現在価格を取得できませんでした。Equity計算に含められません。")

        # グローバル変数に反映
        GLOBAL_TOTAL_EQUITY = total_equity
        
        logging.info(f"✅ 口座ステータス取得成功: Equity={format_usdt(total_equity)} USDT, Free USDT={format_usdt(total_usdt_balance)}")
        
        return {
            'total_usdt_balance': total_usdt_balance,
            'total_equity': total_equity,
            'open_positions': open_ccxt_positions,
            'error': False
        }

    except Exception as e:
        logging.error(f"❌ 口座ステータスの取得中にエラーが発生: {e}", exc_info=True)
        return {'total_usdt_balance': 0.0, 'total_equity': 0.0, 'open_positions': [], 'error': True}


async def initialize_exchange_client():
    """CCXTクライアントを初期化する"""
    global EXCHANGE_CLIENT, IS_CLIENT_READY
    
    if IS_CLIENT_READY:
        return
        
    try:
        exchange_class = getattr(ccxt_async, CCXT_CLIENT_NAME)
        
        # 共通のパラメータを設定
        params = {
            'apiKey': API_KEY,
            'secret': SECRET_KEY,
            'enableRateLimit': True,
        }
        
        # 取引所固有の設定 (例: mexcの現物取引には 'options' が必要)
        if CCXT_CLIENT_NAME.lower() == 'mexc':
            # MEXCの現物取引を確実に指定
            params['options'] = {
                'defaultType': 'spot', 
            }
        
        EXCHANGE_CLIENT = exchange_class(params)
        
        # マーケットデータをロードし、シンボルのリストを取得
        await EXCHANGE_CLIENT.load_markets()
        
        IS_CLIENT_READY = True
        logging.info(f"✅ CCXTクライアント {CCXT_CLIENT_NAME.upper()} の初期化に成功しました。")
        
    except Exception as e:
        logging.critical(f"🚨 CCXTクライアントの初期化中に致命的なエラーが発生: {e}", exc_info=True)
        IS_CLIENT_READY = False


async def fetch_top_symbols() -> List[str]:
    """取引所から出来高上位の銘柄を取得し、監視リストを更新する"""
    global EXCHANGE_CLIENT, IS_CLIENT_READY, DEFAULT_SYMBOLS, TOP_SYMBOL_LIMIT
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.warning("⚠️ クライアントが準備できていないため、出来高上位銘柄の取得をスキップします。")
        return DEFAULT_SYMBOLS.copy()
        
    logging.info("⏳ 出来高上位銘柄の更新を開始...")
    
    try:
        # 全ティッカーを取得
        tickers = await EXCHANGE_CLIENT.fetch_tickers()
        
        # USDTペアかつ出来高情報があるものにフィルタリング
        usdt_tickers = {}
        for symbol, ticker in tickers.items():
            if symbol.endswith('/USDT') and ticker.get('quoteVolume') is not None and ticker.get('quoteVolume') > 0:
                usdt_tickers[symbol] = ticker['quoteVolume']
                
        # 出来高でソート
        sorted_tickers = sorted(usdt_tickers.items(), key=lambda item: item[1], reverse=True)
        
        # TOP Nを取得
        top_symbols = [symbol for symbol, _ in sorted_tickers[:TOP_SYMBOL_LIMIT]]
        
        # DEFAULT_SYMBOLSにTOP銘柄を結合し、重複を削除してからリストに戻す
        combined_symbols = list(set(top_symbols + DEFAULT_SYMBOLS))
        
        logging.info(f"✅ 出来高上位銘柄の更新に成功しました。監視銘柄数: {len(combined_symbols)}")
        return combined_symbols
        
    except Exception as e:
        logging.error(f"❌ 出来高上位銘柄の取得に失敗しました: {e}。デフォルトの銘柄を使用します。")
        return DEFAULT_SYMBOLS.copy()


async def fetch_fgi_data() -> Dict:
    """Fear & Greed Index (FGI) と為替レート（USDX）のデータを取得する"""
    # 外部APIのURL
    FGI_API_URL = "https://api.alternative.me/fng/?limit=1"
    # USDX/DXYの代理としてUSD/JPY (ドル円) の値動きを使用
    FOREX_API_URL = "https://api.binance.com/api/v3/klines?symbol=USDCJPY&interval=1h&limit=50" # USDCJPYを想定
    
    fgi_proxy = 0.0
    fgi_raw_value = 'N/A'
    forex_bonus = 0.0
    
    # 1. Fear & Greed Index (FGI) の取得
    try:
        response = await asyncio.to_thread(requests.get, FGI_API_URL, timeout=5)
        data = response.json()
        
        if data and data.get('data'):
            fgi_value = int(data['data'][0]['value']) # 0 (Extreme Fear) - 100 (Extreme Greed)
            fgi_raw_value = data['data'][0]['value_classification']
            
            # FGIを-1.0から+1.0の範囲に正規化し、感情の強さを表すプロキシとする
            # 50(Neutral)を中心に、0を-1.0、100を+1.0とする
            fgi_proxy = (fgi_value - 50) / 50.0
            
            logging.info(f"✅ FGIデータ取得成功: {fgi_raw_value} (Score: {fgi_value}, Proxy: {fgi_proxy:.2f})")
            
    except Exception as e:
        logging.error(f"❌ FGIデータ取得失敗: {e}")
        
    # 2. 為替レート (USDXの代理) の取得とボーナス計算
    try:
        # BINANCE APIはCCXTクライアントとは独立して使用 (CCXTに為替レートはないため)
        response = await asyncio.to_thread(requests.get, FOREX_API_URL, timeout=5)
        klines = response.json()
        
        if klines and len(klines) >= 2:
            df = pd.DataFrame(klines, columns=['time', 'open', 'high', 'low', 'close', 'volume', 'close_time', 'quote_asset_volume', 'num_trades', 'taker_buy_base_asset_volume', 'taker_buy_quote_asset_volume', 'ignore'])
            df['close'] = df['close'].astype(float)
            
            # 直近20期間の終値の変化率を計算
            recent_change_percent = (df['close'].iloc[-1] - df['close'].iloc[-20]) / df['close'].iloc[-20]
            
            # ドル高傾向 (+0.5%以上) ならリスクオフとしてペナルティ (-0.02)
            if recent_change_percent > 0.005:
                forex_bonus = -0.02 # 2点ペナルティ
            # ドル安傾向 (-0.5%以下) ならリスクオンとしてボーナス (+0.02)
            elif recent_change_percent < -0.005:
                forex_bonus = 0.02 # 2点ボーナス
            # それ以外は中立 (0.0)
            
            # USDX/DXYはマクロコンテキストの調整用として使用。
            # ドル高 -> リスクオフ -> 閾値を厳しくする (スコア -)
            # ドル安 -> リスクオン -> 閾値を緩くする (スコア +)
            logging.info(f"✅ 為替データ取得成功: USDCJPY 20h変化率={recent_change_percent*100:.2f}%, ボーナス={forex_bonus:.2f}")

    except Exception as e:
        logging.error(f"❌ 為替データ取得失敗: {e}")
        
    # マクロコンテキストを返す
    return {'fgi_proxy': fgi_proxy, 'fgi_raw_value': fgi_raw_value, 'forex_bonus': forex_bonus}


async def apply_technical_indicators(df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """ OHLCVデータにテクニカル指標を適用する """
    
    # Simple Moving Average (SMA)
    df['SMA_20'] = ta.sma(df['close'], length=20)
    df['SMA_50'] = ta.sma(df['close'], length=50)
    # 長期SMA (200)
    df['SMA_200'] = ta.sma(df['close'], length=LONG_TERM_SMA_LENGTH) 
    
    # Relative Strength Index (RSI)
    df['RSI'] = ta.rsi(df['close'], length=14)
    
    # Moving Average Convergence Divergence (MACD)
    macd_data = ta.macd(df['close'], fast=12, slow=26, signal=9, append=False)
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
    if bb_data is not None and not bb_data.empty:
        # 動的にキーを特定するために、'BBL'で始まり、最後に'.0'で終わるキーを探す
        bb_lower_key = next((col for col in bb_data.columns if col.startswith('BBL') and col.endswith('.0')), None)
        bb_upper_key = next((col for col in bb_data.columns if col.startswith('BBU') and col.endswith('.0')), None)
        bb_middle_key = next((col for col in bb_data.columns if col.startswith('BBM') and col.endswith('.0')), None)
        
        if bb_lower_key and bb_upper_key and bb_middle_key:
            df['BBL'] = bb_data[bb_lower_key]
            df['BBU'] = bb_data[bb_upper_key]
            df['BBM'] = bb_data[bb_middle_key]
        else:
            logging.error(f"❌ BBANDSのキーを特定できませんでした ({timeframe})。")
            df['BBL'] = np.nan
            df['BBU'] = np.nan
            df['BBM'] = np.nan
    else:
        df['BBL'] = np.nan
        df['BBU'] = np.nan
        df['BBM'] = np.nan

    # Average True Range (ATR)
    df['ATR'] = ta.atr(df['high'], df['low'], df['close'], length=14) 
    
    # On Balance Volume (OBV)
    df['OBV'] = ta.obv(df['close'], df['volume'])
    
    # 直近の出来高 (前回の出来高と比較)
    df['Prev_Volume'] = df['volume'].shift(1)
    
    # Pivot Points (直近のローソク足の情報を取得するために、計算を調整する必要がある)
    # ここでは、単純な高値/安値の構造を使用するため、ピボット計算は省略

    return df

def calculate_score(df: pd.DataFrame, market_ticker: Dict, timeframe: str, macro_context: Dict) -> Tuple[float, Dict, float, float, float]:
    """ テクニカル分析に基づいてロングシグナルのスコアリングを実行する """
    
    if df.empty or len(df) < LONG_TERM_SMA_LENGTH:
        # 必要なデータがない場合はスコアを返さない
        return 0.0, {}, 0.0, 0.0, 0.0
        
    # 最新の確定足のデータを取得 (df[-2]が最新の確定足)
    last_candle = df.iloc[-2]
    current_price = market_ticker['last']
    tech_data = {}
    total_score = 0.0
    
    # ----------------------------------------------------
    # A. テクニカル指標の分析
    # ----------------------------------------------------

    # 1. 長期トレンドフィルタ (SMA200)
    is_above_long_term_sma = current_price > last_candle['SMA_200']
    
    # 2. RSIモメンタム
    rsi_value = last_candle['RSI']
    tech_data['rsi_value'] = rsi_value

    # 3. MACD
    macd_hist = last_candle['MACDh']
    macd_line = last_candle['MACD']
    macd_signal = last_candle['MACDs']
    
    # 4. ボラティリティ
    bb_lower = last_candle['BBL']
    bb_upper = last_candle['BBU']
    bb_middle = last_candle['BBM']
    
    if bb_lower and bb_upper and bb_middle:
        bb_width_ratio = (bb_upper - bb_lower) / bb_middle
        tech_data['bb_width_ratio'] = bb_width_ratio
    else:
        bb_width_ratio = 0.0

    # 5. 出来高
    volume_increase_ratio = last_candle['volume'] / last_candle['Prev_Volume'] if last_candle['Prev_Volume'] > 0 else 0.0
    
    # 6. OBV (上昇モメンタム)
    obv_momentum = last_candle['OBV'] > df.iloc[-3]['OBV'] # 直近でOBVが上昇しているか

    # 7. 価格構造 (直近の高値/安値)
    # 過去N期間の安値 (サポート) - 4期間のローソク足の安値をサポートと見なす
    support_low = df['low'].iloc[-5:-1].min()
    # 過去N期間の高値 (レジスタンス) - 4期間のローソク足の高値をレジスタンスと見なす
    resistance_high = df['high'].iloc[-5:-1].max()
    
    # 8. 流動性 (板の厚み) - スプレッドと板の量を考慮
    bid = market_ticker.get('bid', 0.0)
    ask = market_ticker.get('ask', 0.0)
    
    # スプレッド (Bid/Askの差) の評価 (小さいほど流動性が高い)
    if bid > 0 and ask > 0:
        spread_ratio = (ask - bid) / current_price
        # 例: スプレッドが0.05%未満なら流動性ボーナスを与える
        liquidity_bonus_value = LIQUIDITY_BONUS_MAX if spread_ratio < 0.0005 else 0.0
    else:
        liquidity_bonus_value = 0.0
    tech_data['liquidity_bonus_value'] = liquidity_bonus_value
    
    # ----------------------------------------------------
    # B. スコア計算ロジック
    # ----------------------------------------------------

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
        
        # ただし、長期トレンド逆行ペナルティが最大値であっても、トレンド反転の初動を捉えるために
        # ペナルティを最大値にキャップする
        if long_term_reversal_penalty_value > LONG_TERM_REVERSAL_PENALTY:
            long_term_reversal_penalty_value = LONG_TERM_REVERSAL_PENALTY
            
        total_score -= long_term_reversal_penalty_value
        
    tech_data['long_term_reversal_penalty_value'] = long_term_reversal_penalty_value

    # C. 中期/長期トレンドアライメントボーナス (10点)
    # SMA50がSMA200の上にある (中期的な上昇トレンドの確認)
    trend_alignment_bonus_value = 0.0
    if last_candle['SMA_50'] > last_candle['SMA_200']:
        trend_alignment_bonus_value = TREND_ALIGNMENT_BONUS
        
    total_score += trend_alignment_bonus_value
    tech_data['trend_alignment_bonus_value'] = trend_alignment_bonus_value

    # D. 価格構造/ピボット支持ボーナス (6点)
    # 価格が直近のローソク足の安値（支持線）の上に位置している
    structural_pivot_bonus = 0.0
    # 現在価格が直近4期間の安値よりも2ATR分離れている（十分な安全マージン）
    atr_value = last_candle['ATR']
    if current_price > support_low and (current_price - support_low) > (atr_value * 2.0):
        structural_pivot_bonus = STRUCTURAL_PIVOT_BONUS
        
    total_score += structural_pivot_bonus
    tech_data['structural_pivot_bonus'] = structural_pivot_bonus
    tech_data['support_low'] = support_low

    # E. MACDペナルティ (25点)
    # MACDラインがシグナルラインを下回っている、またはMACDヒストグラムが大きくマイナス (発散)
    macd_penalty_value = 0.0
    if macd_line < macd_signal or macd_hist < 0:
        # 不利なクロスまたは発散ならペナルティ
        macd_penalty_value = MACD_CROSS_PENALTY
        
    total_score -= macd_penalty_value
    tech_data['macd_penalty_value'] = macd_penalty_value

    # F. RSIモメンタムボーナス (10点)
    # RSIがRSI_MOMENTUM_LOW (45)を下回っている状態から、反転上昇し始めている (例: 15期間で最も高い)
    rsi_momentum_bonus_value = 0.0
    if rsi_value < RSI_MOMENTUM_LOW:
        # RSIが低い状態から、直近5期間で最高値の場合にボーナスを適用
        if rsi_value == df['RSI'].iloc[-5:-1].max():
            # 乖離率に応じて可変ボーナス (0.00点〜0.10点)
            # RSIが20に近づくほどボーナスが大きくなる
            max_low_rsi = RSI_MOMENTUM_LOW
            min_low_rsi = 20.0
            
            # (45 - RSI) / (45 - 20) で 0.0 から 1.0 の係数を計算
            if rsi_value < max_low_rsi:
                factor = min((max_low_rsi - rsi_value) / (max_low_rsi - min_low_rsi), 1.0)
                rsi_momentum_bonus_value = RSI_MOMENTUM_BONUS_MAX * factor

    total_score += rsi_momentum_bonus_value
    tech_data['rsi_momentum_bonus_value'] = rsi_momentum_bonus_value
    
    # G. 出来高/OBV確証ボーナス (5点)
    # OBVが上昇傾向にある (買い圧力の確認)
    obv_momentum_bonus_value = 0.0
    if obv_momentum:
        obv_momentum_bonus_value = OBV_MOMENTUM_BONUS
        
    total_score += obv_momentum_bonus_value
    tech_data['obv_momentum_bonus_value'] = obv_momentum_bonus_value

    # H. 出来高スパイクボーナス (7点)
    # 直近の確定足の出来高が前回の出来高の2倍以上の場合にボーナス
    volume_increase_bonus_value = 0.0
    if volume_increase_ratio >= 2.0:
        volume_increase_bonus_value = VOLUME_INCREASE_BONUS
        
    total_score += volume_increase_bonus_value
    tech_data['volume_increase_bonus_value'] = volume_increase_bonus_value

    # I. 流動性ボーナス (7点)
    total_score += liquidity_bonus_value

    # J. ボラティリティペナルティ (低ボラティリティ)
    volatility_penalty_value = 0.0
    if bb_width_ratio > 0.0 and bb_width_ratio < VOLATILITY_BB_PENALTY_THRESHOLD:
        # 例: 1%未満
        volatility_penalty_value = -0.05 # 5点ペナルティ
        
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

    # ----------------------------------------------------
    # C. SL/TPとロットサイズの計算
    # ----------------------------------------------------
    
    # リスクリワード比率の設定 (今回は1:3固定、または動的に計算)
    rr_target = 3.0
    
    # ATRに基づく SL/TP の計算
    # SL: エントリー価格から 1.5 ATR 下
    # TP: エントリー価格から 1.5 * RR_TARGET ATR 上
    entry_price = current_price
    
    # SLは直近のサポート (support_low) を下回る位置に設定、または2.0 ATR下
    # SLは、直近の安値のサポートと、2.0 ATR下のうち、よりタイトな方に設定
    sl_from_atr = entry_price - (atr_value * 2.0)
    
    # SL: max(サポート - マージン, ATRベースSL) で安全性を確保
    # SLは常にエントリー価格より下でなければならない
    stop_loss = max(sl_from_atr, support_low * 0.999) # 1%下のサポートを最低限の基準とする
    
    # TP: RR_TARGET を満たす位置 (Entry + (Entry - SL) * RR_TARGET)
    risk_usdt = entry_price - stop_loss
    take_profit = entry_price + (risk_usdt * rr_target)
    
    # 損切り価格がエントリー価格よりも上になる、またはTP価格がSL価格より下になる異常な状況のチェック
    if stop_loss >= entry_price or take_profit <= entry_price:
        logging.warning(f"⚠️ {market_ticker['symbol']} ({timeframe}): 計算されたSL/TPが不正です。スキップします。")
        return 0.0, {}, 0.0, 0.0, 0.0

    # 5. シグナルデータの組み立て
    signal = {
        'symbol': market_ticker['symbol'],
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

    return signal, final_score, entry_price, stop_loss, take_profit


# ★ 修正箇所: macro_context の型ヒントを Dict に変更
async def fetch_ohlcv_and_analyze(symbol: str, tf: str, limit: int, market_ticker: dict, macro_context: Dict) -> Optional[Dict]:
    """ OHLCVデータを取得し、テクニカル分析とスコアリングを実行する """
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        return None
        
    try:
        # OHLCVデータを取得
        ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(symbol, tf, limit=limit)
        
        if len(ohlcv) < limit:
            logging.warning(f"⚠️ {symbol} ({tf}): 必要なローソク足データ ({limit}本) が揃いませんでした ({len(ohlcv)}本)。スキップします。")
            return None
            
        # DataFrameに変換
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df = df.set_index('timestamp')
        df[['open', 'high', 'low', 'close', 'volume']] = df[['open', 'high', 'low', 'close', 'volume']].astype(float)
        
        # テクニカル指標を適用
        df = await apply_technical_indicators(df, tf)
        
        # スコアを計算 (エラー発生を防ぐため、tupleのまま受け取る)
        result_tuple = calculate_score(df, market_ticker, tf, macro_context)
        signal = result_tuple[0] # 0番目に signal dict が返される

        # 最後に、シグナルから不要なデータを削除して返す
        return signal

    except Exception as e:
        logging.error(f"❌ {symbol} ({tf}) OHLCV取得/分析中にエラーが発生: {e}", exc_info=True)
        return None

async def adjust_order_amount(symbol: str, usdt_amount: float, price: float) -> Tuple[float, float]:
    """
    USDT建ての希望額を、取引所の最小取引単位と精度に合わせて調整し、
    ベース通貨の数量と最終的なUSDT額を返す。
    """
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT or symbol not in EXCHANGE_CLIENT.markets:
        return 0.0, 0.0
        
    market = EXCHANGE_CLIENT.markets[symbol]
    
    # 1. 注文数量の計算 (ベース通貨建て)
    base_amount = usdt_amount / price
    
    # 2. 数量の精度要件 (amount precision)
    precision = market['precision']['amount']
    
    # 3. 最小注文数量 (minAmount)
    min_amount = market['limits']['amount']['min']
    
    # 4. 数量の丸め
    # 小数点以下の桁数をCCXTの精度から取得
    if precision is None:
        # 精度が設定されていない場合は、一旦小数点以下4桁としておく
        precision_digits = 4
    elif isinstance(precision, float) and precision < 1:
        # 例: 0.0001
        precision_digits = max(0, int(-math.log10(precision)))
    elif isinstance(precision, int):
        # 例: 4 (小数第4位)
        precision_digits = precision
    else:
        precision_digits = 4
        
    # 精度桁数で丸め（四捨五入）
    if precision_digits > 0:
        base_amount_rounded = round(base_amount, precision_digits)
    else:
        base_amount_rounded = math.floor(base_amount)
        
    # 5. 最小注文数量のチェック
    if base_amount_rounded < min_amount:
        logging.warning(f"⚠️ {symbol}: 計算数量 ({base_amount_rounded:.8f}) が最小要件 ({min_amount:.8f}) を満たしません。")
        return 0.0, 0.0
        
    final_usdt_amount = base_amount_rounded * price
    return base_amount_rounded, final_usdt_amount


async def place_limit_buy_order_ioc(symbol: str, usdt_amount: float, price: float) -> Dict:
    """
    現物指値買い注文 (IOC: Immediate-Or-Cancel) を実行し、約定結果を返す。
    部分約定でも、約定した分はポジションとして管理する。
    """
    global EXCHANGE_CLIENT
    
    if TEST_MODE:
        # テストモードでは取引を実行せず、擬似的な成功結果を返す
        mock_amount, mock_usdt = await adjust_order_amount(symbol, usdt_amount, price)
        if mock_amount <= 0:
             return {'status': 'error', 'error_message': 'テストモード: 想定ロットが最小取引額未満です。', 'filled_amount': 0.0}

        return {
            'status': 'ok',
            'filled_amount': mock_amount,
            'filled_usdt': mock_usdt,
            'entry_price': price,
            'order_id': f'TEST-{uuid.uuid4().hex}'
        }

    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        return {'status': 'error', 'error_message': 'クライアントが準備できていません。'}

    try:
        # 1. ロットサイズを取引所の要件に合わせて調整
        amount_to_buy, usdt_adjusted = await adjust_order_amount(symbol, usdt_amount, price)
        
        if amount_to_buy <= 0:
            return {'status': 'error', 'error_message': 'ロットサイズが最小取引額を満たしません。'}
            
        # 2. IOC指値買い注文を実行
        # params={'timeInForce': 'IOC'} はCCXTの共通パラメータではないため、
        # 取引所固有のパラメータを使用。Mexcの場合、'timeInForce': 'IOC' が一般的。
        params = {'timeInForce': 'IOC'}
        
        # CCXTの create_order は現物(spot)注文
        order = await EXCHANGE_CLIENT.create_order(
            symbol=symbol,
            type='limit', # 指値
            side='buy',
            amount=amount_to_buy,
            price=price,
            params=params
        )
        
        # 3. 約定結果を確認 (IOC/FOKでは 'filled' または 'partially filled' のみ)
        filled_amount = order.get('filled', 0.0)
        
        if filled_amount > 0:
            # 約定した場合 (部分約定も含む)
            filled_usdt = filled_amount * price
            logging.info(f"✅ {symbol} 指値買いIOC注文完了: {filled_amount:.4f} @ {price:.4f} (USDT: {filled_usdt:.2f})")
            return {
                'status': 'ok',
                'filled_amount': filled_amount,
                'filled_usdt': filled_usdt,
                'entry_price': price,
                'order_id': order.get('id')
            }
        else:
            # 即時約定しなかった (IOCによりキャンセルされている)
            logging.warning(f"⚠️ {symbol} 指値買いIOC注文: 約定せずキャンセルされました。")
            return {
                'status': 'error',
                'error_message': f'指値買い注文 ({format_price_precision(price)}) が即時約定しませんでした (filled: 0.0)。',
                'filled_amount': 0.0
            }

    except ccxt.NetworkError as e:
        logging.error(f"❌ ネットワークエラー ({symbol}): {e}", exc_info=True)
        return {'status': 'error', 'error_message': f'ネットワークエラー: {e}'}
    except ccxt.ExchangeError as e:
        logging.error(f"❌ 取引所エラー ({symbol}): {e}", exc_info=True)
        # 稀に取引所エラーでキャンセルされる場合もある
        return {'status': 'error', 'error_message': f'取引所エラー: {e}'}
    except Exception as e:
        logging.error(f"❌ 不明なエラー ({symbol}): {e}", exc_info=True)
        return {'status': 'error', 'error_message': f'不明なエラー: {e}'}


async def place_sl_tp_orders(
    symbol: str, 
    filled_amount: float, 
    stop_loss: float, 
    take_profit: float
) -> Dict:
    """ 
    現物ポジションのストップロス (SL) とテイクプロフィット (TP) 注文を同時に設定する。
    SL/TPには、トリガー価格と注文価格が必要となる (トリガー指値注文)。
    """
    global EXCHANGE_CLIENT
    
    if TEST_MODE:
        return {
            'status': 'ok',
            'sl_order_id': f'TEST-SL-{uuid.uuid4().hex}',
            'tp_order_id': f'TEST-TP-{uuid.uuid4().hex}',
            'filled_amount': filled_amount,
            'filled_usdt': filled_amount * take_profit
        }
        
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        return {'status': 'error', 'error_message': 'クライアントが準備できていません。'}
        
    sl_order_id = None
    tp_order_id = None
    
    # 1. TP注文 (テイクプロフィット: 指値売り) の設定
    try:
        # TP: take_profitを指値価格として指値売り注文
        # トリガー価格は不要、単なる指値売り
        tp_order = await EXCHANGE_CLIENT.create_order(
            symbol=symbol,
            type='limit', # 指値
            side='sell',
            amount=filled_amount,
            price=take_profit
        )
        tp_order_id = tp_order.get('id')
        logging.info(f"✅ TP注文成功: ID={tp_order_id}, Price={format_price_precision(take_profit)}")
    except Exception as e:
        logging.critical(f"🚨 TP注文失敗 ({symbol}): {e}", exc_info=True)
        return {'status': 'error', 'error_message': f'TP注文失敗: {e}'}

    # 2. SL注文 (ストップロス: トリガー価格での指値売り) の設定
    try:
        # SL: ストップロス注文 (トリガー価格で指値売り)
        # 多くの取引所では 'stop limit' または 'stop market' を使用。
        # ここでは指値注文をトリガーするストップリミット注文を想定する (Mexcの例)
        
        # トリガー価格: ストップロス価格に設定
        sl_trigger_price = stop_loss 
        # 注文価格: トリガー価格よりも少し低く設定 (スリッページ防止)
        sl_limit_price = stop_loss * 0.999 # 0.1%下の指値
        
        # CCXTの標準に合わせるため、'stop limit' typeを使用
        # 注: 各取引所APIによりパラメータが異なるため、ここは要調整。
        # Mexcの現物では 'stop_loss_limit' がサポートされている
        params = {
            'stopPrice': sl_trigger_price, # トリガー価格
        }
        
        sl_order = await EXCHANGE_CLIENT.create_order(
            symbol=symbol,
            type='limit', # 指値注文 (CCXTはタイプによって自動的に適切なエンドポイントにルーティング)
            side='sell',
            amount=filled_amount,
            price=sl_limit_price, # 注文価格
            params=params
        )
        
        sl_order_id = sl_order.get('id')
        logging.info(f"✅ SL注文成功: ID={sl_order_id}, Trigger Price={format_price_precision(sl_trigger_price)}")
        
    except Exception as e:
        logging.critical(f"🚨 SL注文失敗 ({symbol}): {e}", exc_info=True)
        # 🚨 SL注文失敗は致命的。TP注文をキャンセルし、ポジションを強制クローズする
        try:
            if tp_order_id:
                await EXCHANGE_CLIENT.cancel_order(tp_order_id, symbol)
                logging.warning(f"⚠️ TP注文 (ID: {tp_order_id}) をキャンセルしました。")
        except Exception as cancel_e:
            logging.error(f"❌ TP注文のキャンセルにも失敗: {cancel_e}")
            
        return {'status': 'error', 'error_message': f'SL注文失敗: {e}'}

    return {
        'status': 'ok',
        'sl_order_id': sl_order_id,
        'tp_order_id': tp_order_id,
        'filled_amount': filled_amount,
        'filled_usdt': filled_amount * take_profit # TP価格で概算のUSDT額を返す（エントリー価格ではないが、後続処理で再計算するため問題なし）
    }


async def close_position_immediately(symbol: str, amount: float) -> Dict:
    """ 不完全なポジションを成行売りで即座にクローズする (リカバリ用) """
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY or amount <= 0:
        return {'status': 'skipped', 'error_message': 'スキップ: クライアント未準備または数量がゼロです。'}
        
    logging.warning(f"🚨 不完全ポジションの強制クローズを試みます: {symbol} (数量: {amount:.4f})")

    try:
        # 数量の丸め（成行注文でも精度は重要）
        # amount はベース通貨量 (例: BTC)
        # 成行注文では価格が不要だが、`adjust_order_amount`を使用するため、現在価格が必要。
        # しかし、ここでは既にamountが確定しているので、CCXTの amount_to_precision を使用するのが安全
        
        market = EXCHANGE_CLIENT.markets[symbol]
        
        # 注文数量を取引所の精度に丸める (ccxtの共通メソッドを使用)
        amount_to_sell = EXCHANGE_CLIENT.amount_to_precision(symbol, amount)
        
        # 成行売り注文
        close_order = await EXCHANGE_CLIENT.create_order(
            symbol=symbol,
            type='market',
            side='sell',
            amount=amount_to_sell # 調整後の数量
        )
        
        closed_amount = close_order.get('filled', 0.0)
        
        if closed_amount > 0:
            logging.info(f"✅ 不完全ポジションの強制クローズ成功: {symbol} ({closed_amount:.4f})")
            return {
                'status': 'ok',
                'closed_amount': closed_amount
            }
        else:
            # 成行売りでも約定しなかった場合 (極めて稀)
            logging.error(f"❌ 強制クローズの成行売り注文が約定しませんでした。")
            return {
                'status': 'error',
                'error_message': '成行売り注文で約定が発生しませんでした。'
            }

    except Exception as e:
        logging.critical(f"🚨 強制クローズ失敗 ({symbol}): {e}", exc_info=True)
        return {'status': 'error', 'error_message': f'強制クローズ中のエラー: {e}'}


async def execute_trade(signal: Dict, account_status: Dict) -> Dict:
    """
    シグナルに基づいて取引を実行し、成功すればSL/TPを設定し、
    ポジションリストに追加する。
    """
    global EXCHANGE_CLIENT, OPEN_POSITIONS, GLOBAL_TOTAL_EQUITY
    
    symbol = signal['symbol']
    entry_price = signal['entry_price']
    stop_loss = signal['stop_loss']
    take_profit = signal['take_profit']
    score = signal['score']
    
    # 1. 動的ロットサイズの計算
    total_equity = account_status['total_equity']
    
    # スコアに応じてロットサイズを調整
    if total_equity > 0:
        # スコアを DYNAMIC_LOT_SCORE_MAX (0.96) で最大化するように線形補間
        score_factor = min(max(score - SIGNAL_THRESHOLD_NORMAL, 0) / (DYNAMIC_LOT_SCORE_MAX - SIGNAL_THRESHOLD_NORMAL), 1.0)
        
        # 最小ロット (10%) と最大ロット (20%) の間で調整
        lot_percent = DYNAMIC_LOT_MIN_PERCENT + (DYNAMIC_LOT_MAX_PERCENT - DYNAMIC_LOT_MIN_PERCENT) * score_factor
        usdt_amount = total_equity * lot_percent
        
        # 最小取引額BASE_TRADE_SIZE_USDTも下回らないようにする
        if usdt_amount < BASE_TRADE_SIZE_USDT:
            usdt_amount = BASE_TRADE_SIZE_USDT
        
    else:
        # 総資産額が不明な場合はベースサイズを使用
        usdt_amount = BASE_TRADE_SIZE_USDT
        
    # 信号にロットサイズを記録 (通知用)
    signal['lot_size_usdt'] = usdt_amount
    
    # 2. ポジションの購入を実行 (IOC指値買い)
    trade_result = await place_limit_buy_order_ioc(symbol, usdt_amount, entry_price)
    
    if trade_result['status'] == 'ok' and trade_result['filled_amount'] > 0:
        # 3. 約定した数量と金額を取得
        filled_amount = trade_result['filled_amount']
        filled_usdt = trade_result['filled_usdt']
        
        # 4. SL/TP注文を設定
        sl_tp_result = await place_sl_tp_orders(symbol, filled_amount, stop_loss, take_profit)
        
        if sl_tp_result['status'] == 'ok':
            # 5. ポジションを管理リストに追加
            new_position = {
                'id': trade_result['order_id'], # 買い注文のIDをポジションIDとする
                'symbol': symbol,
                'entry_price': entry_price,
                'filled_amount': filled_amount,
                'filled_usdt': filled_usdt,
                'stop_loss': stop_loss,
                'take_profit': take_profit,
                'sl_order_id': sl_tp_result['sl_order_id'],
                'tp_order_id': sl_tp_result['tp_order_id'],
                'timestamp': time.time()
            }
            OPEN_POSITIONS.append(new_position)
            logging.info(f"✅ 取引成功: {symbol} にポジションを追加しました。")
            
            return {
                'status': 'ok',
                'filled_amount': filled_amount,
                'filled_usdt': filled_usdt,
                'entry_price': entry_price,
                'sl_order_id': sl_tp_result['sl_order_id'],
                'tp_order_id': sl_tp_result['tp_order_id']
            }
        else:
            # 🚨 SL/TP設定失敗 -> ポジションを強制クローズ
            error_message = sl_tp_result.get('error_message', 'SL/TP設定中に不明なエラー')
            logging.critical(f"🚨 {symbol}: SL/TP設定失敗。ポジションを強制クローズします。: {error_message}")
            
            # 強制クローズを実行
            close_result = await close_position_immediately(symbol, filled_amount)
            
            return {
                'status': 'error',
                'error_message': f'取引成功後のリカバリー失敗: {error_message}',
                'close_status': close_result['status'],
                'closed_amount': close_result.get('closed_amount', 0.0),
                'close_error_message': close_result.get('error_message'),
            }
            
    else:
        # 💡 即時約定しなかった (IOC/FOKでフィルされなかった)
        error_message = trade_result.get('error_message', '指値注文が約定しませんでした。')
        logging.warning(f"⚠️ {symbol}: {error_message}")
        return {'status': 'error', 'error_message': error_message, 'close_status': 'skipped'}

    except ccxt.NetworkError as e:
        error_message = f"ネットワークエラー: {e}"
        logging.error(f"❌ 取引実行失敗 ({symbol}): {error_message}", exc_info=True)
        return {'status': 'error', 'error_message': error_message, 'close_status': 'skipped'}
    except ccxt.ExchangeError as e:
        error_message = f"取引所エラー: {e}"
        logging.error(f"❌ 取引実行失敗 ({symbol}): {error_message}", exc_info=True)
        # 💡 CCXTエラーでも約定している可能性を考慮:
        # このエラーが約定処理後に発生した場合は、ポジションが残る可能性があるが、
        # ここでは create_order の結果で判断しているため、一旦スキップとして扱う
        return {'status': 'error', 'error_message': error_message, 'close_status': 'skipped'}
    except Exception as e:
        error_message = f"予期せぬエラー: {e}"
        logging.error(f"❌ 取引実行失敗 ({symbol}): {error_message}", exc_info=True)
        return {'status': 'error', 'error_message': error_message, 'close_status': 'skipped'}

# ----------------------------------------------------
# 注文監視・決済ループ
# ----------------------------------------------------

async def open_order_management_loop():
    """ オープンポジションと注文のステータスを監視するループ (10秒ごと) """
    global OPEN_POSITIONS, GLOBAL_TOTAL_EQUITY
    
    while True:
        try:
            if not IS_CLIENT_READY or not OPEN_POSITIONS:
                await asyncio.sleep(MONITOR_INTERVAL)
                continue
                
            tasks = []
            for position in OPEN_POSITIONS:
                tasks.append(check_and_manage_position(position))
                
            # 全ての監視タスクを並行して実行
            await asyncio.gather(*tasks)
            
        except Exception as e:
            logging.critical(f"🚨 オーダー管理ループ中に致命的なエラーが発生: {e}", exc_info=True)
            
        await asyncio.sleep(MONITOR_INTERVAL)


async def check_and_manage_position(position: Dict):
    """ 個別のポジションのSL/TP注文の状態をチェックし、決済処理を行う """
    global EXCHANGE_CLIENT, OPEN_POSITIONS, GLOBAL_TOTAL_EQUITY
    
    symbol = position['symbol']
    sl_order_id = position['sl_order_id']
    tp_order_id = position['tp_order_id']
    
    # 1. SL/TP注文のステータスを取得
    sl_status = None
    tp_status = None
    
    try:
        # SL/TPの注文は取引所のAPIコールが必要
        if sl_order_id and sl_order_id.startswith('TEST-SL-'):
            sl_status = {'status': 'open'} # テストモードでは常にオープンと見なす
        elif sl_order_id:
            sl_status = await EXCHANGE_CLIENT.fetch_order(sl_order_id, symbol)
            
        if tp_order_id and tp_order_id.startswith('TEST-TP-'):
            tp_status = {'status': 'open'} # テストモードでは常にオープンと見なす
        elif tp_order_id:
            tp_status = await EXCHANGE_CLIENT.fetch_order(tp_order_id, symbol)

    except ccxt.OrderNotFound:
        # 注文が約定済み、または取引所側でキャンセルされた可能性がある
        # ここでは、後に約定しているかどうかを確認する
        logging.warning(f"⚠️ {symbol}: SL/TP注文が取引所で見つかりません (ID: {sl_order_id}/{tp_order_id})。約定を確認します。")
        # 注文が見つからない場合は status=None のまま続行

    except Exception as e:
        logging.error(f"❌ {symbol}: SL/TPステータス取得中にエラー: {e}")
        return # エラー時は次のループを待つ

    # 2. SLまたはTPが約定したかを確認
    is_sl_closed = sl_status and sl_status.get('status') == 'closed' and sl_status.get('filled', 0.0) > 0
    is_tp_closed = tp_status and tp_status.get('status') == 'closed' and tp_status.get('filled', 0.0) > 0
    is_closed = is_sl_closed or is_tp_closed

    if is_closed:
        exit_type = "SL約定" if is_sl_closed else "TP約定"
        
        # 既にポジションがクローズされている場合、管理リストから削除し、通知を送信
        logging.info(f"🛑 {symbol}: ポジションがクローズされました ({exit_type})。")

        # 決済情報を構築
        exit_order = sl_status if is_sl_closed else tp_status
        exit_price = exit_order.get('price', exit_order.get('average', exit_order.get('cost', 0.0) / exit_order.get('filled', 1.0)))
        filled_amount = position['filled_amount']
        entry_price = position['entry_price']
        
        # PnLの計算
        pnl_usdt = (exit_price - entry_price) * filled_amount
        pnl_percent = (pnl_usdt / position['filled_usdt']) * 100 if position['filled_usdt'] > 0 else 0.0
        
        # 決済シグナルを作成 (通知用)
        close_signal = position.copy()
        close_signal['exit_price'] = exit_price
        close_signal['pnl_usdt'] = pnl_usdt
        close_signal['pnl_percent'] = pnl_percent
        close_signal['exit_type'] = exit_type
        
        # ログと通知
        log_signal(close_signal, "ポジション決済")
        await send_telegram_message(format_telegram_message(
            close_signal, 
            "ポジション決済", 
            get_current_threshold(GLOBAL_MACRO_CONTEXT), 
            trade_result=close_signal, 
            exit_type=exit_type
        ))
        
        # 決済したポジションをリストから削除
        OPEN_POSITIONS = [p for p in OPEN_POSITIONS if p['id'] != position['id']]
        
        # 約定しなかった残りの注文をキャンセル
        if is_tp_closed and sl_order_id:
            try:
                # TP約定 -> SL注文をキャンセル
                await EXCHANGE_CLIENT.cancel_order(sl_order_id, symbol)
                logging.info(f"✅ TP約定に伴い、SL注文 (ID: {sl_order_id}) をキャンセルしました。")
            except Exception as e:
                logging.error(f"❌ SL注文のキャンセル失敗 ({symbol}): {e}")
                
        elif is_sl_closed and tp_order_id:
            try:
                # SL約定 -> TP注文をキャンセル
                await EXCHANGE_CLIENT.cancel_order(tp_order_id, symbol)
                logging.info(f"✅ SL約定に伴い、TP注文 (ID: {tp_order_id}) をキャンセルしました。")
            except Exception as e:
                logging.error(f"❌ TP注文のキャンセル失敗 ({symbol}): {e}")

    # ★ 3. SL/TPが片方または両方存在しない場合の再設定ロジック (V19.0.53で追加)
    sl_open = sl_status and sl_status['status'] == 'open'
    tp_open = tp_status and tp_status['status'] == 'open'

    if not is_closed and (not sl_open or not tp_open):
        logging.warning(f"⚠️ {symbol}: SL({sl_order_id}:{sl_status.get('status') if sl_status else 'N/A'}) または TP({tp_order_id}:{tp_status.get('status') if tp_status else 'N/A'}) の注文が欠落しています。再設定を試みます。")
        
        # まず、残っている注文があればキャンセルする (二重注文防止)
        if sl_open:
            try:
                await EXCHANGE_CLIENT.cancel_order(sl_order_id, symbol)
                logging.info(f"✅ SL再設定のため、既存SL注文 (ID: {sl_order_id}) をキャンセルしました。")
            except Exception as e:
                logging.error(f"❌ 既存SL注文のキャンセル失敗 ({symbol}): {e}")
        
        if tp_open:
            try:
                await EXCHANGE_CLIENT.cancel_order(tp_order_id, symbol)
                logging.info(f"✅ TP再設定のため、既存TP注文 (ID: {tp_order_id}) をキャンセルしました。")
            except Exception as e:
                logging.error(f"❌ 既存TP注文のキャンセル失敗 ({symbol}): {e}")

        # SL/TPを再設定
        re_place_result = await place_sl_tp_orders(
            symbol=symbol,
            filled_amount=position['filled_amount'],
            stop_loss=position['stop_loss'],
            take_profit=position['take_profit']
        )
        
        if re_place_result['status'] == 'ok':
            # 注文IDを更新
            position['sl_order_id'] = re_place_result['sl_order_id']
            position['tp_order_id'] = re_place_result['tp_order_id']
            logging.info(f"✅ {symbol}: SL/TP注文の再設定に成功しました。")
        else:
            logging.critical(f"🚨 {symbol}: SL/TP注文の再設定に失敗しました。ポジション ({position['id']}) の監視を継続しますが、手動での確認が必要です。")


# ----------------------------------------------------
# メインの分析・取引ロジック
# ----------------------------------------------------

# ★ 修正箇所: analyze_symbol関数の定義に macro_context を追加
async def analyze_symbol(symbol: str, market_ticker: dict, macro_context: Dict) -> List[Dict]:
    """ 指定された銘柄の分析とシグナル生成を行う """
    global LAST_SIGNAL_TIME, TRADE_SIGNAL_COOLDOWN, HOURLY_ATTEMPT_LOG, OPEN_POSITIONS
    
    signals = []
    
    # ポジションを既に保有している場合はスキップ (同一銘柄の二重エントリー防止)
    if any(p['symbol'] == symbol for p in OPEN_POSITIONS):
        HOURLY_ATTEMPT_LOG[symbol] = "保有中"
        return signals
        
    # クールダウン期間中の場合はスキップ
    if symbol in LAST_SIGNAL_TIME and \
       (time.time() - LAST_SIGNAL_TIME[symbol] < TRADE_SIGNAL_COOLDOWN):
        HOURLY_ATTEMPT_LOG[symbol] = "クールダウン"
        return signals
        
    for tf in TARGET_TIMEFRAMES:
        limit = REQUIRED_OHLCV_LIMITS[tf]
        try:
            # macro_context を引数として渡す
            signal = await fetch_ohlcv_and_analyze(symbol, tf, limit, market_ticker, macro_context) 
            
            if signal and signal['score'] >= 0.50: # ベーススコア以上のシグナルのみを返す
                signals.append(signal)
        except Exception as e:
            logging.error(f"❌ {symbol} ({tf}) の分析中にエラーが発生: {e}", exc_info=True)
            
    return signals


async def main_bot_loop():
    """ボットのメイン実行ループ (1分ごと)"""
    global LAST_SUCCESS_TIME, LAST_SIGNAL_TIME, LAST_ANALYSIS_SIGNALS, CURRENT_MONITOR_SYMBOLS, GLOBAL_MACRO_CONTEXT, LAST_HOURLY_NOTIFICATION_TIME, IS_FIRST_MAIN_LOOP_COMPLETED, HOURLY_SIGNAL_LOG, HOURLY_ATTEMPT_LOG, BOT_VERSION
    
    # メインループの初回起動時のみ、クライアント初期化とステータス通知を実行
    if not IS_CLIENT_READY:
        await initialize_exchange_client()
        # 初期化失敗時は即座にリターンし、次のループを待つ
        if not IS_CLIENT_READY:
            logging.critical("🚨 クライアント初期化失敗のため、メインBOTループをスキップします。")
            await asyncio.sleep(LOOP_INTERVAL)
            return
            # return await main_bot_loop() # 再帰呼び出しは避ける
        
    start_time = time.time()
    now_jst = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")
    logging.info(f"--- 💡 {now_jst} - BOT LOOP START (M1 Frequency) ---")
    
    try:
        # 1. FGIデータを取得し、グローバルマクロコンテキストを更新
        GLOBAL_MACRO_CONTEXT = await fetch_fgi_data()
        
        # FGIの値をスコアリングに反映する準備
        # macro_influence_score = (GLOBAL_MACRO_CONTEXT.get('fgi_proxy', 0.0) + GLOBAL_MACRO_CONTEXT.get('forex_bonus', 0.0))
        current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT) # 動的閾値を決定
        
        # 2. 口座ステータスを取得し、新規取引の可否をチェック
        account_status = await fetch_account_status()
        if account_status.get('error'):
            logging.critical("🚨 口座ステータス取得失敗。新規取引をスキップします。")
            # 継続するためにエラーをリセット
            account_status = {'total_usdt_balance': 0.0, 'total_equity': 0.0, 'open_positions': [], 'error': False}
        
        # 3. 監視銘柄リストの更新 (一定時間ごと、または初回のみ)
        if time.time() - LAST_SUCCESS_TIME > LOOP_INTERVAL * 5 or not IS_FIRST_MAIN_LOOP_COMPLETED:
            if not SKIP_MARKET_UPDATE:
                CURRENT_MONITOR_SYMBOLS = await fetch_top_symbols()
            else:
                logging.warning("⚠️ SKIP_MARKET_UPDATEが有効です。デフォルト銘柄を使用します。")
                CURRENT_MONITOR_SYMBOLS = DEFAULT_SYMBOLS.copy()

        # 4. 全銘柄のティッカー情報を取得
        market_tickers = await EXCHANGE_CLIENT.fetch_tickers(CURRENT_MONITOR_SYMBOLS)
        
        # 5. 全監視銘柄の分析を並行して実行
        tasks = []
        HOURLY_ATTEMPT_LOG = {} # 1時間ごとのレポートのためにリセット
        for symbol in CURRENT_MONITOR_SYMBOLS:
            # USDTペアではない、またはティッカーがない場合はスキップ
            if symbol not in market_tickers or market_tickers[symbol]['last'] is None:
                HOURLY_ATTEMPT_LOG[symbol] = "ティッカーなし"
                continue
                
            # ★ 修正箇所: analyze_symbol に GLOBAL_MACRO_CONTEXT を渡す
            tasks.append(analyze_symbol(symbol, market_tickers[symbol], GLOBAL_MACRO_CONTEXT)) 

        all_signals = await asyncio.gather(*tasks)
        
        # 6. 結果を平坦化し、スコアでソート
        valid_signals = [signal for sublist in all_signals if sublist for signal in sublist if signal]
        valid_signals.sort(key=lambda x: x['score'], reverse=True)
        
        LAST_ANALYSIS_SIGNALS = valid_signals[:TOP_SIGNAL_COUNT] # Top Nのみを記録
        
        # 7. 最適なシグナルを取得し、取引を実行
        best_signal = LAST_ANALYSIS_SIGNALS[0] if LAST_ANALYSIS_SIGNALS else None
        
        if best_signal and best_signal['score'] >= current_threshold:
            # 取引条件を満たした場合
            
            # クールダウンチェック (analyze_symbolでスキップされているはずだが、念のため二重チェック)
            can_trade = True
            error_message = ""
            
            if TEST_MODE:
                logging.warning(f"⚠️ {best_signal['symbol']} 取引スキップ: TEST_MODEが有効です。")
                can_trade = False
            
            # ポジション保有チェック
            elif any(p['symbol'] == best_signal['symbol'] for p in OPEN_POSITIONS):
                error_message = f"ポジションを既に保有しています。二重エントリーをスキップします。"
                logging.warning(f"⚠️ {best_signal['symbol']} 取引スキップ: {error_message}")
                can_trade = False
                
            # クールダウンチェック
            elif best_signal['symbol'] in LAST_SIGNAL_TIME and \
                 (time.time() - LAST_SIGNAL_TIME[best_signal['symbol']] < TRADE_SIGNAL_COOLDOWN):
                error_message = f"クールダウン期間中です (次回取引可能: {datetime.fromtimestamp(LAST_SIGNAL_TIME[best_signal['symbol']] + TRADE_SIGNAL_COOLDOWN, JST).strftime('%H:%M:%S')} JST)"
                logging.warning(f"⚠️ {best_signal['symbol']} 取引スキップ: {error_message}")
                can_trade = False
                
            # 残高チェック
            elif account_status['total_usdt_balance'] < MIN_USDT_BALANCE_FOR_TRADE:
                error_message = f"残高不足 (現在: {format_usdt(account_status['total_usdt_balance'])} USDT)。新規取引に必要な額: {MIN_USDT_BALANCE_FOR_TRADE:.2f} USDT。"
                logging.warning(f"⚠️ {best_signal['symbol']} 取引スキップ: {error_message}")
                can_trade = False
                
            if can_trade:
                # 取引実行
                trade_result = await execute_trade(best_signal, account_status)
                
                # 取引結果に基づいて通知を送信
                await send_telegram_message(format_telegram_message(
                    best_signal, 
                    "取引シグナル", 
                    current_threshold, 
                    trade_result=trade_result
                ))
                
                if trade_result['status'] == 'ok':
                    LAST_SIGNAL_TIME[best_signal['symbol']] = time.time()
                    log_signal(best_signal, "取引成功")
                    HOURLY_SIGNAL_LOG.append(best_signal)
                else:
                    log_signal({**best_signal, **trade_result}, "取引失敗")
                    HOURLY_SIGNAL_LOG.append({**best_signal, 'score': 0.0}) # 失敗シグナルはスコア0として記録
            
            else:
                # 取引スキップの場合の処理 (通知は行わないが、ログに記録)
                trade_result = {'status': 'error', 'error_message': error_message, 'close_status': 'skipped'}
                log_signal({**best_signal, **trade_result}, "取引スキップ")
        
        else:
            if best_signal:
                logging.info(f"ℹ️ {best_signal['symbol']} の最高スコア ({best_signal['score']*100:.2f}) が閾値 ({current_threshold*100:.2f}) 未満です。取引をスキップします。")
                log_signal(best_signal, "閾値未満スキップ")

        # 8. Hourly Reportの送信
        if time.time() - LAST_HOURLY_NOTIFICATION_TIME >= HOURLY_SCORE_REPORT_INTERVAL or not IS_FIRST_MAIN_LOOP_COMPLETED:
            report_message = format_hourly_report(valid_signals, HOURLY_ATTEMPT_LOG, start_time, current_threshold, BOT_VERSION)
            await send_telegram_message(report_message)
            LAST_HOURLY_NOTIFICATION_TIME = time.time()
            HOURLY_SIGNAL_LOG = [] # リセット
            
        # 9. 初回ループ完了フラグ
        if not IS_FIRST_MAIN_LOOP_COMPLETED:
            # 初回起動通知
            startup_message = format_startup_message(
                account_status, 
                GLOBAL_MACRO_CONTEXT, 
                len(CURRENT_MONITOR_SYMBOLS), 
                current_threshold, 
                BOT_VERSION
            )
            await send_telegram_message(startup_message)
            IS_FIRST_MAIN_LOOP_COMPLETED = True
            
        LAST_SUCCESS_TIME = time.time()

    except Exception as e:
        now_jst = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")
        logging.critical(f"🚨 メインBOTループ中に致命的なエラーが発生: {e}", exc_info=True)
        
        # 致命的エラー通知 (クールダウンを無視してすぐに送信)
        error_message = (
            f"🚨 **致命的なエラー通知** - {now_jst} (JST)\n"
            f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
            f"  - **エラー内容**: <code>{e}</code>\n"
            f"  - **BOTバージョン**: <code>{BOT_VERSION}</code>\n"
            f"  - **アクション**: メインループを再開します。\n"
            f"<code>- - - - - - - - - - - - - - - - - - - - -</code>"
        )
        await send_telegram_message(error_message)
        logging.info(f"✅ 致命的エラー通知を送信しました (Ver: {BOT_VERSION})")
        
    finally:
        end_time = time.time()
        elapsed_time = end_time - start_time
        time_to_sleep = max(0.0, LOOP_INTERVAL - elapsed_time)
        
        # クライアントがオープンでなければクローズする (メモリリーク防止)
        if EXCHANGE_CLIENT:
            await EXCHANGE_CLIENT.close()
            
        # 次のループまで待機
        await asyncio.sleep(time_to_sleep)
        await main_bot_loop() # 次のループをスケジュール


# ====================================================================================
# FastAPI (Health Check / Status Check)
# ====================================================================================

app = FastAPI()

@app.get("/")
async def read_root():
    """ ヘルスチェック用のルート """
    return {"status": "ok", "version": BOT_VERSION, "client": CCXT_CLIENT_NAME.upper(), "test_mode": TEST_MODE, "is_ready": IS_CLIENT_READY}

@app.get("/status")
async def get_status():
    """ ボットの現在の状態を返す (JSON) """
    global GLOBAL_MACRO_CONTEXT, GLOBAL_TOTAL_EQUITY
    
    status_data = {
        "status": "Running" if IS_FIRST_MAIN_LOOP_COMPLETED else "Starting",
        "bot_version": BOT_VERSION,
        "exchange": CCXT_CLIENT_NAME.upper(),
        "is_ready": IS_CLIENT_READY,
        "last_success_time_jst": datetime.fromtimestamp(LAST_SUCCESS_TIME, JST).strftime("%Y/%m/%d %H:%M:%S") if LAST_SUCCESS_TIME > 0 else "N/A",
        "total_equity": GLOBAL_TOTAL_EQUITY,
        "macro_context": GLOBAL_MACRO_CONTEXT,
        "current_signal_threshold": get_current_threshold(GLOBAL_MACRO_CONTEXT),
        "monitoring_symbols_count": len(CURRENT_MONITOR_SYMBOLS),
        "open_positions_count": len(OPEN_POSITIONS),
        "last_signals": [
            {
                "symbol": s['symbol'], 
                "timeframe": s['timeframe'], 
                "score": f"{s['score']*100:.2f}",
                "rr_ratio": f"1:{s['rr_ratio']:.2f}",
                "entry_price": format_price_precision(s['entry_price']),
                "current_price": format_price_precision(s['current_price'])
            } 
            for s in LAST_ANALYSIS_SIGNALS
        ],
        "open_positions": [
            {
                "symbol": p['symbol'],
                "entry_price": format_price_precision(p['entry_price']),
                "filled_amount": f"{p['filled_amount']:.4f}",
                "sl": format_price_precision(p['stop_loss']),\
                "tp": format_price_precision(p['take_profit']),
                "id": p['id'][:8] + '...'
            }
            for p in OPEN_POSITIONS
        ],
    }
    
    # JSONResponseを使用して、意図的にHTMLタグをエンコードせずに返す
    return JSONResponse(content=status_data)


# 💡 メインループのタスクを起動するイベントハンドラ
@app.on_event("startup")
async def start_bot_tasks():
    """アプリケーション起動時にBOTタスクを開始する"""
    global MONITOR_INTERVAL
    logging.info("💡 アプリケーション起動イベントを検出しました。BOTタスクを開始します。")
    
    # メインBOTループを非同期で開始
    asyncio.create_task(main_bot_loop())
    
    # オーダー管理ループを非同期で開始
    asyncio.create_task(open_order_management_loop())
    
    logging.info("✅ BOTタスク（メインループと注文管理）の開始をスケジュールしました。")


if __name__ == "__main__":
    # このブロックはUvicornが直接呼び出すのではなく、uvicorn main_render:app コマンドで実行される
    # Uvicornの起動コマンドは実行環境に依存するため、ここではログのみ出力
    pass
