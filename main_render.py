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
        
        if exit_type_final == 'SL約定':
            trade_status_line = "🔴 **ポジション決済**: SL(損切り)約定"
        elif exit_type_final == 'TP約定':
            trade_status_line = "🟢 **ポジション決済**: TP(利確)約定"
        elif exit_type_final == '再設定クローズ':
            trade_status_line = "🟡 **ポジション決済**: SL/TP再設定前の強制クローズ"
        else:
            trade_status_line = "⚫ **ポジション決済**: 手動/システムクローズ"
            
        pnl_usdt = trade_result.get('pnl_usdt', 0.0)
        pnl_percent = trade_result.get('pnl_percent', 0.0)
        
        if pnl_usdt >= 0:
            pnl_display = f"🟢 +{format_usdt(pnl_usdt)} USDT (+{pnl_percent:.2f}%)"
        else:
            pnl_display = f"🔴 {format_usdt(pnl_usdt)} USDT ({pnl_percent:.2f}%)"
            
        trade_section = (
            f"📈 **決済結果**\n"
            f"  - **決済タイプ**: <code>{exit_type_final}</code>\n"
            f"  - **エントリー価格**: <code>{format_price_precision(trade_result.get('entry_price', 0.0))}</code>\n"
            f"  - **決済価格**: <code>{format_price_precision(trade_result.get('exit_price', 0.0))}</code>\n"
            f"  - **損益**: **{pnl_display}**\n"
            f"  - **残高 (Equity)**: <code>{format_usdt(trade_result.get('total_equity_after', GLOBAL_TOTAL_EQUITY))}</code> USDT\n"
        )
    
    # メインメッセージ構成
    message = (
        f"🔔 **{context}** ({now_jst})\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"**{trade_status_line}**\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"🪙 **銘柄情報**\n"
        f"  - **シンボル**: <code>{symbol}</code>\n"
        f"  - **時間軸**: <code>{timeframe}</code>\n"
        f"  - **スコア**: <code>{score*100:.2f} / 100</code> (閾値: {current_threshold*100:.0f})\n"
        f"  - **推定勝率**: <code>{estimated_wr}</code>\n"
        f"  - **リスクリワード (RR)**: <code>1:{rr_ratio:.1f}</code>\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"📈 **取引計画**\n"
        f"  - **エントリー**: <code>{format_price_precision(entry_price)}</code>\n"
        f"  - **ストップロス (SL)**: <code>{format_price_precision(stop_loss)}</code>\n"
        f"  - **テイクプロフィット (TP)**: <code>{format_price_precision(take_profit)}</code>\n"
    )
    
    # 推定損益ライン
    message += est_pnl_line
    
    message += f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
    
    # 取引実行結果セクションの追加
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
        f"📈 **【Hourly Report】** ({start_jst} - {now_jst})\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - **総試行銘柄数**: <code>{total_attempted}</code> 銘柄\n"
        f"  - **分析成功**: <code>{analyzed_count}</code> 銘柄\n"
        f"  - **データ不足等でスキップ**: <code>{skipped_count}</code> 銘柄\n"
        f"  - **取引閾値 (Score)**: <code>{current_threshold*100:.0f} / 100</code>\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n\n"
    )

    # 1. BEST SIGNAL
    best_signal = signals_sorted[0] if signals_sorted else None
    if best_signal:
        message += (
            f"👑 **最高スコア銘柄**\n"
            f"  - **シンボル**: <code>{best_signal['symbol']}</code>\n"
            f"  - **スコア**: <code>{best_signal['score']*100:.2f} / 100</code>\n"
            f"  - **時間軸**: <code>{best_signal['timeframe']}</code>\n"
            f"  - **推定勝率**: <code>{get_estimated_win_rate(best_signal['score'])}</code>\n"
            f"  - **RR**: <code>1:{best_signal['rr_ratio']:.1f}</code>\n"
        )
    else:
        message += "👑 **最高スコア銘柄**: 該当なし\n"

    # 2. WORST SIGNAL (最低スコアのシグナル、ただし閾値超えのシグナルの中で最低のもの)
    # ここでは、単純に全シグナルの中で最低のものを選ぶ
    worst_signal = signals_sorted[-1] if signals_sorted else None
    if worst_signal and worst_signal['symbol'] != best_signal['symbol']:
        message += (
            f"\n\n📉 **最低スコア銘柄**\n"
            f"  - **シンボル**: <code>{worst_signal['symbol']}</code>\n"
            f"  - **スコア**: <code>{worst_signal['score']*100:.2f} / 100</code>\n"
            f"  - **時間軸**: <code>{worst_signal['timeframe']}</code>\n"
            f"  - **推定勝率**: <code>{get_estimated_win_rate(worst_signal['score'])}</code>\n"
            f"  - **RR**: <code>1:{worst_signal['rr_ratio']:.1f}</code>\n"
        )
    
    # 3. スキップされた銘柄のリスト (最大5つ)
    if attempt_log:
        message += f"\n\n⚠️ **データ不足等でスキップされた銘柄 (最大5)**\n"
        for i, (symbol, reason) in enumerate(list(attempt_log.items())[:5]):
            message += f"  - <code>{symbol}</code>: {reason}\n"
        if len(attempt_log) > 5:
            message += f"  - ...他 {len(attempt_log) - 5} 銘柄\n"

    # 4. ベースラインを超えたシグナル (最大3つ)
    qualified_signals = [s for s in signals_sorted if s['score'] >= current_threshold]
    if qualified_signals:
        message += f"\n\n✨ **取引閾値を超えたシグナル** ({len(qualified_signals)} 銘柄)\n"
        for i, sig in enumerate(qualified_signals[:3]):
            message += (
                f"  - <code>{sig['symbol']}</code> ({sig['timeframe']}): **{sig['score']*100:.2f}** 点 (Entry: {format_price_precision(sig['entry_price'])})\n"
            )
        if len(qualified_signals) > 3:
            message += f"  - ...他 {len(qualified_signals) - 3} 銘柄\n"

    # 5. 取引計画 (最高スコア銘柄のSL/TP表示) - (これは最高スコア銘柄に統合されたため削除または調整が必要だが、ここでは元のコード構造を維持)
    if best_signal:
        message += (
            f"\n\n⚙️ **取引計画 (最高スコア)**\n"
            f"  - **エントリー (Entry)**: <code>{format_price_precision(best_signal['entry_price'])}</code>\n"
            f"  - **SL/TP**: <code>{format_price_precision(best_signal['stop_loss'])}</code> / <code>{format_price_precision(best_signal['take_profit'])}</code>\n"
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
        'filled_amount': signal.get('filled_amount'),
        'lot_size_usdt': signal.get('lot_size_usdt'),
        'tech_data': _to_json_compatible(signal.get('tech_data', {}))
    }
    
    # JSONL形式でファイルに追記
    try:
        # ロギングはここでは省略し、コンソールログのみとする
        # with open('apex_trade_log.jsonl', 'a', encoding='utf-8') as f:
        #     f.write(json.dumps(log_data, ensure_ascii=False) + '\n')
        pass # ファイルロギングはスキップ
    except Exception as e:
        logging.error(f"❌ シグナルログのファイル書き込み中にエラーが発生: {e}")

async def send_telegram_notification(message: str):
    """Telegramにメッセージを送信する"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.warning("⚠️ TelegramのトークンまたはChat IDが設定されていません。通知をスキップします。")
        return
        
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        'chat_id': TELEGRAM_CHAT_ID,
        'text': message,
        'parse_mode': 'HTML', # HTML形式でメッセージをパース
        'disable_web_page_preview': True
    }
    
    # Telegram APIへのリクエストを同期的に実行 (asyncio.to_threadを使用)
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, lambda: requests.post(url, data=payload, timeout=10))

async def fetch_top_symbols(limit: int = TOP_SYMBOL_LIMIT) -> List[str]:
    """取引所から出来高上位の現物シンボルを取得する"""
    global EXCHANGE_CLIENT
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.warning("⚠️ CCXTクライアントが準備できていません。デフォルトシンボルを使用します。")
        return DEFAULT_SYMBOLS
        
    if SKIP_MARKET_UPDATE:
        logging.info("ℹ️ SKIP_MARKET_UPDATEがTrueのため、出来高上位銘柄の更新をスキップします。")
        return DEFAULT_SYMBOLS

    logging.info("⏳ 出来高上位銘柄を更新中...")
    try:
        # すべてのティッカーを取得
        tickers = await EXCHANGE_CLIENT.fetch_tickers()
        
        # USDTペアかつ出来高情報があるもののみをフィルタリング
        usdt_pairs = {
            symbol: data for symbol, data in tickers.items() 
            if symbol.endswith('/USDT') and 'quoteVolume' in data and data['quoteVolume'] is not None
        }
        
        # quoteVolume（USDT建の出来高）で降順にソート
        sorted_pairs = sorted(
            usdt_pairs.items(), 
            key=lambda item: item[1]['quoteVolume'], 
            reverse=True
        )
        
        # 上位N件を取得し、DEFAULT_SYMBOLSに追加（重複排除）
        top_symbols = [symbol for symbol, _ in sorted_pairs[:limit]]
        
        # デフォルトシンボルとマージし、ユニークなリストを作成
        final_symbols = list(set(top_symbols + DEFAULT_SYMBOLS))
        
        logging.info(f"✅ 出来高上位銘柄の更新に成功: TOP {len(top_symbols)} 銘柄を含め、合計 {len(final_symbols)} 銘柄を監視対象とします。")
        return final_symbols
        
    except Exception as e:
        logging.error(f"❌ 出来高上位銘柄の取得に失敗しました: {e}。デフォルトシンボルを使用します。", exc_info=True)
        return DEFAULT_SYMBOLS
    
async def fetch_account_status() -> Dict:
    """口座のUSDT残高と総資産額（Equity）を取得する"""
    global EXCHANGE_CLIENT, GLOBAL_TOTAL_EQUITY
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        return {'total_usdt_balance': 0.0, 'total_equity': 0.0, 'open_positions': [], 'error': True}

    try:
        # 1. 残高情報の取得
        balance = await EXCHANGE_CLIENT.fetch_balance()
        
        # free/totalのUSDT残高を取得
        total_usdt_balance = balance.get('total', {}).get('USDT', 0.0)
        
        # 2. 総資産額 (Equity) の計算
        total_equity = total_usdt_balance # まずUSDTをベースに
        
        # USDT以外の保有資産を評価
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
        return {
            'total_usdt_balance': 0.0,
            'total_equity': GLOBAL_TOTAL_EQUITY, # 最後に成功した値または初期値を返す
            'open_positions': [],
            'error': True
        }

async def fetch_fgi_data() -> Dict:
    """外部APIからFGI（恐怖・貪欲指数）と為替（USDX）データを取得し、スコアリング用のプロキシを計算する"""
    fgi_proxy = 0.0
    fgi_raw_value = 'N/A'
    forex_bonus = 0.0
    
    # 1. FGI (Crypto Fear & Greed Index)
    try:
        fgi_url = "https://api.alternative.me/fng/?limit=1"
        # 外部APIへのリクエストを同期的に実行 (asyncio.to_threadを使用)
        loop = asyncio.get_event_loop()
        fgi_response = await loop.run_in_executor(None, lambda: requests.get(fgi_url, timeout=5))
        fgi_response.raise_for_status()
        fgi_data = fgi_response.json().get('data', [])
        
        if fgi_data:
            value = int(fgi_data[0]['value'])
            fgi_raw_value = f"{value} ({fgi_data[0]['value_classification']})"
            
            # FGIスコアを -0.5から+0.5の範囲に正規化 (0:Neutral, 100:Extreme Greed, 0:Extreme Fear)
            fgi_proxy = (value - 50) / 100.0 # -0.5 to +0.5
            
            logging.info(f"✅ FGIデータ取得成功: {fgi_raw_value} -> Proxy={fgi_proxy:.2f}")
        else:
            logging.warning("⚠️ FGIデータが空です。")
            
    except Exception as e:
        logging.error(f"❌ FGIデータ取得中にエラーが発生: {e}")
        # 失敗した場合は、全て0.0を返す
        pass
        
    # 2. 為替データ (USDX / DXY) - 取得はここではスキップし、固定値または別APIを使用 (簡略化)
    # ここでは仮のロジックとして、USDXが高いとリスクオフ（マイナス）、低いとリスクオン（プラス）と仮定する
    # forex_bonus = -0.01 * (fgi_proxy) # 例：FGIが高ければ（Greed）、さらに為替ボーナスをマイナスにするなど。
    forex_bonus = 0.0 # 一旦無効化

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
    if bb_data is not None and not bb_data.empty:
        df['BBL'] = bb_data.iloc[:, 0] # Lower band
        df['BBM'] = bb_data.iloc[:, 1] # Middle band (SMA20)
        df['BBU'] = bb_data.iloc[:, 2] # Upper band
        # BBW: Bollinger Band Width (ボラティリティ指標)
        df['BBW'] = ((df['BBU'] - df['BBL']) / df['BBM']) 
    else:
        df['BBL'] = np.nan
        df['BBM'] = np.nan
        df['BBU'] = np.nan
        df['BBW'] = np.nan
        
    # Average True Range (ATR) - ボラティリティ指標
    df['ATR'] = ta.atr(df['high'], df['low'], df['close'], length=14)
    
    # On-Balance Volume (OBV)
    df['OBV'] = ta.obv(df['close'], df['volume'])
    
    # ピボットポイントの計算 (Pivot Points) - 4Hのデータを使用することが望ましいが、ここでは終値ベースで計算
    # 実際には、より上位の時間軸のデータを使うべきだが、簡略化のためR1/S1のみを計算
    
    # 前日のHLCを取得
    # df['PP'] = (df['high'].shift(1) + df['low'].shift(1) + df['close'].shift(1)) / 3
    # df['R1'] = 2 * df['PP'] - df['low'].shift(1)
    # df['S1'] = 2 * df['PP'] - df['high'].shift(1)
    
    # 出来高の移動平均 (ボリュームの異常を検出するため)
    df['VOLUME_SMA_50'] = ta.sma(df['volume'], length=50)
    
    return df.dropna(subset=['SMA_200', 'RSI', 'MACD', 'ATR', 'OBV']) # 必要な指標が揃っている行のみを返す

def analyze_symbol(symbol: str, timeframe: str, macro_context: Dict) -> Optional[Dict]:
    """特定のシンボルと時間枠でテクニカル分析を行い、取引シグナルを生成する"""
    global EXCHANGE_CLIENT
    
    # 1. OHLCVデータの取得
    ohlcv = await fetch_ohlcv_and_analyze(symbol, timeframe)
    if ohlcv is None or ohlcv.empty:
        # logging.warning(f"⚠️ {symbol} ({timeframe}): データ取得失敗またはデータ不足。")
        return None
        
    df = ohlcv.iloc[-5:] # 最新の5つのローソク足のみを分析に利用 (特に最新の足)
    if len(df) < 2:
        # logging.warning(f"⚠️ {symbol} ({timeframe}): 分析に必要なローソク足の数が不足 (5未満)。")
        return None
        
    last_candle = df.iloc[-1]
    prev_candle = df.iloc[-2]
    current_price = last_candle['close']
    
    # 2. ロングシグナルの条件チェック (ここでは、単純なエンベロープ/トレンドフォローを仮定)
    
    # a. 長期トレンドフィルター (SMA200を上回っていること)
    is_above_long_term_sma = last_candle['close'] > last_candle['SMA_200']
    
    # b. 中期トレンドアライメント (SMA50がSMA200を上回っていること)
    is_mid_term_aligned = last_candle['SMA_50'] > last_candle['SMA_200']
    
    # c. 価格構造/ピボットサポート (価格がBB下限またはS1付近で反発していること - 簡略化)
    # 価格がBB下限に近いこと、かつ前の足が下ヒゲで終わっている（反発）
    is_close_to_bbl = last_candle['close'] < last_candle['BBL'] * 1.05 # BB下限から5%以内
    is_reversal_candle = last_candle['low'] < prev_candle['low'] and last_candle['close'] > last_candle['open'] # 陽線で反発
    
    # d. モメンタム条件 (RSIが買われすぎではないが、勢いがある - 例: 45以下から反転)
    is_rsi_momentum_low = last_candle['RSI'] < RSI_MOMENTUM_LOW
    
    # e. 出来高確認 (出来高が50SMAより高いこと)
    is_volume_spike = last_candle['volume'] > last_candle['VOLUME_SMA_50'] * 1.5 # 1.5倍以上の出来高
    
    # f. MACDクロス (MACDラインがシグナルラインを上回っていること)
    is_macd_cross_up = last_candle['MACD'] > last_candle['MACDs'] and last_candle['MACDh'] > prev_candle['MACDh']
    
    # g. OBVの確証 (OBVが上昇傾向にあること)
    # 簡易的に、直近5本のOBVの傾きがプラス
    obv_slope = np.polyfit(range(len(df['OBV'])), df['OBV'].values, 1)[0]
    is_obv_confirming = obv_slope > 0
    
    # 3. エントリー価格、SL、TPの計算 (現行価格をエントリー価格とする)
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
    
    # C. 中期トレンドアライメントボーナス (10点)
    trend_alignment_bonus_value = 0.0
    if is_mid_term_aligned:
        trend_alignment_bonus_value = TREND_ALIGNMENT_BONUS
    total_score += trend_alignment_bonus_value
    tech_data['trend_alignment_bonus_value'] = trend_alignment_bonus_value
    
    # D. 価格構造/ピボット支持ボーナス (6点)
    structural_pivot_bonus = 0.0
    if is_close_to_bbl and is_reversal_candle:
        structural_pivot_bonus = STRUCTURAL_PIVOT_BONUS
    total_score += structural_pivot_bonus
    tech_data['structural_pivot_bonus'] = structural_pivot_bonus
    
    # E. MACDペナルティ (25点)
    macd_penalty_value = 0.0
    if not is_macd_cross_up:
        # MACDが不利な状態（デッドクロスや下向き）であればペナルティ
        # 単純に、MACDラインがシグナルラインより下で、ヒストグラムがマイナスの場合
        if last_candle['MACD'] < last_candle['MACDs'] and last_candle['MACDh'] < 0:
             macd_penalty_value = MACD_CROSS_PENALTY
    total_score -= macd_penalty_value
    tech_data['macd_penalty_value'] = macd_penalty_value
    
    # F. RSIモメンタムボーナス (10点) - 可変ボーナス
    rsi_momentum_bonus_value = 0.0
    if is_rsi_momentum_low:
        # RSIが低いほど（30に近づくほど）ボーナスを最大化 (50/45から30への線形補間)
        max_rsi_diff = RSI_MOMENTUM_LOW - 30.0
        current_rsi_diff = max(0, RSI_MOMENTUM_LOW - last_candle['RSI'])
        
        if max_rsi_diff > 0:
            rsi_factor = min(current_rsi_diff / max_rsi_diff, 1.0)
            # RSIが低いことと、RSIが直近で上昇していること（勢い）を組み合わせる
            is_rsi_rising = last_candle['RSI'] > prev_candle['RSI']
            
            if is_rsi_rising:
                rsi_momentum_bonus_value = RSI_MOMENTUM_BONUS_MAX * rsi_factor
            
    total_score += rsi_momentum_bonus_value
    tech_data['rsi_momentum_bonus_value'] = rsi_momentum_bonus_value
    tech_data['rsi_value'] = last_candle['RSI']
    
    # G. OBVモメンタムボーナス (5点)
    obv_momentum_bonus_value = 0.0
    if is_obv_confirming:
        obv_momentum_bonus_value = OBV_MOMENTUM_BONUS
    total_score += obv_momentum_bonus_value
    tech_data['obv_momentum_bonus_value'] = obv_momentum_bonus_value
    
    # H. 出来高スパイクボーナス (7点)
    volume_increase_bonus_value = 0.0
    if is_volume_spike:
        volume_increase_bonus_value = VOLUME_INCREASE_BONUS
    total_score += volume_increase_bonus_value
    tech_data['volume_increase_bonus_value'] = volume_increase_bonus_value
    
    # I. 低ボラティリティペナルティ (BBWが狭すぎる場合 - 0.01 = 1%未満)
    volatility_penalty_value = 0.0
    if last_candle['BBW'] < VOLATILITY_BB_PENALTY_THRESHOLD:
        volatility_penalty_value = -0.10 # 10点ペナルティ
    total_score += volatility_penalty_value
    tech_data['volatility_penalty_value'] = volatility_penalty_value
    tech_data['bbw_value'] = last_candle['BBW']

    # J. 流動性ボーナス (7点) - 簡易的に、出来高がSMA50を大きく超えている場合を流動性優位とする
    liquidity_bonus_value = 0.0
    # 直近の出来高が過去50期間の平均の2倍以上
    if last_candle['volume'] > last_candle['VOLUME_SMA_50'] * 2.0:
        liquidity_bonus_value = LIQUIDITY_BONUS_MAX
    total_score += liquidity_bonus_value
    tech_data['liquidity_bonus_value'] = liquidity_bonus_value

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
    return signal

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

    # 3. 最大ロットサイズ (総資産のX%)
    max_usdt_lot = total_equity * DYNAMIC_LOT_MAX_PERCENT
    
    # 最小ロットサイズ (総資産のY%) - ロットが小さくなりすぎないように下限を設定
    min_dynamic_lot = total_equity * DYNAMIC_LOT_MIN_PERCENT
    
    # 最小ロットはBASE_TRADE_SIZE_USDTとmin_dynamic_lotの大きい方
    base_lot = max(min_usdt_lot, min_dynamic_lot)
    
    # 4. スコアに応じたロットサイズの調整
    # スコアがDYNAMIC_LOT_SCORE_MAX (例: 0.96) で最大ロット、
    # 閾値 (例: 0.84) でベースロットとなるように線形補間
    current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
    
    if score <= current_threshold:
        # 閾値以下の場合は最小ロット（ベースロット）
        calculated_lot = base_lot
    elif score >= DYNAMIC_LOT_SCORE_MAX:
        # 最大スコア以上の場合は最大ロット
        calculated_lot = max_usdt_lot
    else:
        # 閾値と最大スコアの間で線形補間
        score_range = DYNAMIC_LOT_SCORE_MAX - current_threshold
        lot_range = max_usdt_lot - base_lot
        
        if score_range <= 0 or lot_range <= 0:
            # 計算範囲が無効な場合やロット幅がない場合はベースロット
            calculated_lot = base_lot
        else:
            score_factor = (score - current_threshold) / score_range
            calculated_lot = base_lot + (lot_range * score_factor)

    # 5. 結果のクリッピングと調整
    final_lot = min(max(calculated_lot, min_usdt_lot), max_usdt_lot)
    
    # 10 USDT未満のロットは最小ロットに強制
    if final_lot < 10.0:
        return BASE_TRADE_SIZE_USDT # or 10.0
        
    return final_lot

async def adjust_order_amount(symbol: str, target_usdt: float, price: float) -> Tuple[float, float]:
    """
    目標USDT額と価格に基づき、取引所の精度に合わせて注文数量を調整する。
    Returns: (調整後の数量, 調整後のUSDT金額)
    """
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY or symbol not in EXCHANGE_CLIENT.markets:
        logging.error(f"❌ 注文数量調整失敗: クライアント未準備またはシンボル {symbol} が無効です。")
        return 0.0, 0.0
        
    market = EXCHANGE_CLIENT.markets[symbol]
    
    # 1. 目標数量の計算 (未調整)
    target_amount = target_usdt / price
    
    # 2. 数量の精度 (amount_precision) に合わせて丸める
    amount_precision = market.get('precision', {}).get('amount', 4) # デフォルトは小数第4位
    base_amount_rounded = EXCHANGE_CLIENT.amount_to_precision(symbol, target_amount)
    base_amount_rounded = float(base_amount_rounded)
    
    # 3. 最小注文数量のチェック (min_amount)
    min_amount = market.get('limits', {}).get('amount', {}).get('min', 0.0)
    if base_amount_rounded < min_amount:
        logging.warning(f"⚠️ {symbol}: 調整後の数量 {base_amount_rounded:.8f} が最小注文数量 {min_amount:.8f} 未満です。スキップします。")
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
        return {'status': 'error', 'error_message': f'TP注文失敗: {e}'}

    # 2. SL (ストップロス) ストップリミット売り注文の設定 (Stop Limit Sell)
    try:
        # 取引所に応じて、ストップロス注文のタイプが異なる (Stop Limit, Stop Marketなど)
        # MEXC (CCXT) では、paramsに 'stopLossPrice' を指定することでStop Limit Sellを設定できることが多い
        sl_order = await EXCHANGE_CLIENT.create_order(
            symbol=symbol,
            type='stop_limit', # または 'limit' に param で stopPrice を付与
            side='sell',
            amount=amount_to_sell,
            price=stop_loss * 0.99, # 99%の価格でリミット注文 (取引所に応じて調整が必要)
            params={
                'stopPrice': stop_loss, # 損切りを発動させる価格
                'clientOrderId': f'SL-{uuid.uuid4()}'
            }
        )
        sl_order_id = sl_order['id']
        logging.info(f"✅ SL注文成功: ID={sl_order_id}, StopPrice={format_price_precision(stop_loss)}")

    except Exception as e:
        # SL設定失敗は致命的
        logging.critical(f"❌ SL注文失敗 ({symbol}): {e}", exc_info=True)
        # 既に設定されているTP注文をキャンセルする
        if tp_order_id:
            try:
                await EXCHANGE_CLIENT.cancel_order(tp_order_id, symbol)
                logging.warning(f"⚠️ SL注文失敗に伴い、TP注文 (ID: {tp_order_id}) をキャンセルしました。")
            except Exception as cancel_e:
                logging.error(f"❌ TP注文のキャンセルにも失敗: {cancel_e}")
                
        return {'status': 'error', 'error_message': f'SL注文失敗: {e}'}

    return {'status': 'ok', 'sl_order_id': sl_order_id, 'tp_order_id': tp_order_id}


async def close_position_immediately(symbol: str, amount: float) -> Dict:
    """取引失敗時などに、残ってしまったポジションを成行売りで強制的にクローズする"""
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY or amount <= 0:
        return {'status': 'skipped', 'error_message': 'スキップ: クライアント未準備または数量がゼロです。'}

    logging.warning(f"🚨 不完全ポジションの強制クローズを試みます: {symbol} (数量: {amount:.4f})")
    
    try:
        # 数量の丸め（成行注文でも精度は重要）
        # amount * 1.01 は概算のUSDT価値を推定するため。ここでは、単に amount_to_precision を使う
        amount_to_sell = EXCHANGE_CLIENT.amount_to_precision(symbol, amount)
        amount_to_sell = float(amount_to_sell)

        if amount_to_sell == 0:
            return {'status': 'skipped', 'error_message': 'スキップ: 調整後の数量がゼロです。'}

        # 成行売り注文
        close_order = await EXCHANGE_CLIENT.create_order(
            symbol=symbol,
            type='market',
            side='sell',
            amount=amount_to_sell
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
    
    # 1. 動的ロットサイズの計算
    lot_size_usdt = calculate_dynamic_lot_size(signal['score'], account_status)
    signal['lot_size_usdt'] = lot_size_usdt # シグナルデータにロットサイズを保存

    # 2. 注文数量の調整
    # 指値価格(entry_price)で、lot_size_usdtを元にした数量を計算し、取引所精度に丸める
    base_amount_to_buy, final_usdt_to_buy = await adjust_order_amount(symbol, lot_size_usdt, entry_price)
    
    if base_amount_to_buy <= 0 or final_usdt_to_buy < MIN_USDT_BALANCE_FOR_TRADE:
        return {'status': 'error', 'error_message': '最小取引額または精度チェック失敗により注文スキップ。', 'close_status': 'skipped'}
        
    logging.info(f"⏳ 注文実行: {symbol} - Lot={format_usdt(final_usdt_to_buy)} USDT (Qty: {base_amount_to_buy:.4f}) @ {format_price_precision(entry_price)}")

    # 3. 現物指値買い注文 (IOC - Immediate Or Cancel) を発注
    # IOCを使うことで、すぐに約定しなかった場合はキャンセルされ、不完全なポジションを抱えるリスクを減らす
    buy_order = None
    try:
        buy_order = await EXCHANGE_CLIENT.create_order(
            symbol=symbol,
            type='limit', # 指値
            side='buy',
            amount=base_amount_to_buy,
            price=entry_price,
            params={
                'timeInForce': 'IOC', # 即時約定しなかった場合はキャンセル
                'clientOrderId': f'ENTRY-{uuid.uuid4()}'
            }
        )
        
        # 4. 約定結果のチェック
        filled_amount = buy_order.get('filled', 0.0)
        filled_usdt = buy_order.get('cost', 0.0) # 約定コスト (USDT)
        
        if filled_amount > 0 and filled_usdt > MIN_USDT_BALANCE_FOR_TRADE:
            logging.info(f"✅ 約定成功: {symbol} - {filled_amount:.4f} 数量 @ {format_usdt(filled_usdt)} USDT")
            
            # 5. SL/TP注文の発注
            sl_tp_result = await place_sl_tp_orders(
                symbol=symbol,
                filled_amount=filled_amount,
                stop_loss=signal['stop_loss'],
                take_profit=signal['take_profit']
            )
            
            if sl_tp_result['status'] == 'ok':
                # 6. ポジションをグローバルリストに追加
                new_position = {
                    'id': str(uuid.uuid4()), # ボット内部管理ID
                    'symbol': symbol,
                    'entry_price': buy_order.get('average', entry_price), # 平均約定価格
                    'filled_amount': filled_amount,
                    'filled_usdt': filled_usdt,
                    'stop_loss': signal['stop_loss'],
                    'take_profit': signal['take_profit'],
                    'sl_order_id': sl_tp_result['sl_order_id'],
                    'tp_order_id': sl_tp_result['tp_order_id'],
                    'timestamp': time.time()
                }
                OPEN_POSITIONS.append(new_position)
                
                return {
                    'status': 'ok',
                    'filled_amount': filled_amount,
                    'filled_usdt': filled_usdt,
                    'entry_price': new_position['entry_price'],
                    'sl_order_id': new_position['sl_order_id'],
                    'tp_order_id': new_position['tp_order_id'],
                    'close_status': 'skipped' # クローズは実行していない
                }
            else:
                # 7. SL/TP設定失敗: 既に購入したポジションを即時クローズする
                logging.error(f"❌ SL/TP注文設定失敗: {sl_tp_result['error_message']}。ポジションを強制クローズします。")
                close_result = await close_position_immediately(symbol, filled_amount)
                return {
                    'status': 'error',
                    'error_message': f"指値買い注文成功、しかしSL/TP設定失敗: {sl_tp_result['error_message']}",
                    'close_status': close_result['status'],
                    'closed_amount': close_result.get('closed_amount', 0.0),
                    'close_error_message': close_result.get('error_message'),
                }
                
        else:
            # IOC注文で即時約定しなかった場合（一部約定も最小額未満ならスキップ）
            error_message = f"指値買い注文が約定しませんでした (Filled: {filled_amount:.4f} / Cost: {format_usdt(filled_usdt)} USDT)。"
            logging.warning(f"⚠️ 取引スキップ: {symbol} - {error_message}")
            return {'status': 'error', 'error_message': error_message, 'close_status': 'skipped'}

    except ccxt.InvalidOrder as e:
        error_message = f"無効な注文: {e}"
        logging.error(f"❌ 取引実行失敗 ({symbol}): {error_message}", exc_info=True)
        return {'status': 'error', 'error_message': error_message, 'close_status': 'skipped'}
    except ccxt.InsufficientFunds as e:
        error_message = f"残高不足: {e}"
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

    logging.debug(f"ℹ️ オープン注文監視ループ開始... ({len(OPEN_POSITIONS)} 銘柄)")

    positions_to_remove_ids = []
    
    for position in OPEN_POSITIONS:
        symbol = position['symbol']
        sl_order_id = position['sl_order_id']
        tp_order_id = position['tp_order_id']
        is_closed = False
        exit_type = None
        sl_status = None
        tp_status = None
        
        # 1. SL注文の確認
        sl_status = await check_order_status(sl_order_id, symbol)
        
        if sl_status and sl_status.get('status') == 'closed':
            # SL注文がクローズされている = SLが約定した可能性が高い
            # CCXTの 'closed' は、取引所側で約定またはキャンセルを意味する。
            # 確実な判定は 'filled' または 'amount' == 'filled' で行う
            if sl_status.get('filled', 0.0) > 0:
                is_closed = True
                exit_type = 'SL約定'
                logging.warning(f"🔴 ポジション決済検出: {symbol} - SL約定")
            else:
                 # 注文が約定せずにキャンセルされた可能性 (例: TPが先に約定)
                 sl_open = False
        else:
            sl_open = True
            
        # 2. TP注文の確認 (SLが約定していない場合のみ)
        if not is_closed:
            tp_status = await check_order_status(tp_order_id, symbol)
            
            if tp_status and tp_status.get('status') == 'closed':
                if tp_status.get('filled', 0.0) > 0:
                    is_closed = True
                    exit_type = 'TP約定'
                    logging.info(f"🟢 ポジション決済検出: {symbol} - TP約定")
                else:
                    # 注文が約定せずにキャンセルされた可能性
                    tp_open = False
            else:
                tp_open = True
        
        # 3. 【v19.0.53の追加ロジック】 SL/TPの片方または両方が欠けている場合の再設定
        # - SLが閉じている (sl_open=False) が約定していない (is_closed=False) か、
        # - TPが閉じている (tp_open=False) が約定していない (is_closed=False) 場合。
        # これは、片方が約定して他方がキャンセルされたケースで、まだポジションが残っている可能性がある
        if not is_closed and (not sl_open or not tp_open):
            logging.warning(f"⚠️ {symbol} - SL/TPの注文状態に不一致を検出 (SL Open: {sl_open}, TP Open: {tp_open})。")
            
            # ポジション残量を再確認 (fetch_balanceは重いので、ここではスキップし、一旦強制クローズを試みる)
            
            # ⚠️ 強制クローズの前に、残っている注文をキャンセル
            if sl_open:
                try:
                    await EXCHANGE_CLIENT.cancel_order(sl_order_id, symbol)
                    logging.info(f"✅ 残っているSL注文 ({sl_order_id}) をキャンセルしました。")
                except Exception as e:
                    logging.warning(f"❌ SL注文 ({sl_order_id}) のキャンセルに失敗: {e}")
            if tp_open:
                try:
                    await EXCHANGE_CLIENT.cancel_order(tp_order_id, symbol)
                    logging.info(f"✅ 残っているTP注文 ({tp_order_id}) をキャンセルしました。")
                except Exception as e:
                    logging.warning(f"❌ TP注文 ({tp_order_id}) のキャンセルに失敗: {e}")
                    
            # ここでは、簡略化のため、再設定ではなく強制クローズ (ボット管理外のポジションを残さないため)
            # 実際のポジション残量の確認は複雑なため、ここでは filled_amount を使ってクローズを試みる
            amount_to_close = position['filled_amount']
            close_result = await close_position_immediately(symbol, amount_to_close)
            
            if close_result['status'] == 'ok' and close_result.get('closed_amount', 0.0) > 0:
                 is_closed = True
                 exit_type = '再設定クローズ'
                 logging.info(f"✅ {symbol} - 不完全ポジションを強制クローズしました (再設定クローズ)。")
            elif close_result['status'] == 'error':
                 logging.critical(f"🚨 {symbol} - 不完全ポジションの強制クローズに失敗しました。手動で確認してください。")
                 # エラーがあっても、ポジションはリストから削除（手動介入が必要）
                 positions_to_remove_ids.append(position['id'])
                 continue # 次のポジションへ
            else:
                 logging.info(f"ℹ️ {symbol} - 強制クローズを試みましたが、ポジションは存在しませんでした。")
                 is_closed = True # ポジションは存在しないと見なす

        if not is_closed:
            # SL/TP両方がオープン中
            # logging.debug(f"ℹ️ {symbol} は引き続きオープン中 (SL: {sl_open}, TP: {tp_open})")
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
                notification_message = format_telegram_message(
                    signal=signal_for_log,
                    context="ポジション決済",
                    current_threshold=get_current_threshold(GLOBAL_MACRO_CONTEXT),
                    trade_result=closed_result,
                    exit_type=exit_type # exit_typeを明示的に渡す
                )
                await send_telegram_notification(notification_message)
                log_signal(closed_result, "ポジション決済")

            except Exception as e:
                logging.critical(f"❌ 決済処理中に致命的なエラーが発生 ({symbol}): {e}", exc_info=True)
                # ポジションはリストから削除
                pass

    # 4. 決済されたポジションをリストから削除
    OPEN_POSITIONS = [p for p in OPEN_POSITIONS if p['id'] not in positions_to_remove_ids]
    
    logging.debug(f"ℹ️ オープン注文監視ループ完了。残り {len(OPEN_POSITIONS)} 銘柄。")


async def fetch_ohlcv_and_analyze(symbol: str, timeframe: str) -> Optional[pd.DataFrame]:
    """OHLCVデータを取得し、テクニカル指標を計算する"""
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.error("❌ CCXTクライアントが準備できていません。")
        return None
        
    required_limit = REQUIRED_OHLCV_LIMITS.get(timeframe, 500)
    
    try:
        # OHLCVデータの取得
        ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(
            symbol=symbol,
            timeframe=timeframe,
            limit=required_limit
        )
        
        if not ohlcv or len(ohlcv) < required_limit:
            # データ不足または取得失敗
            return None
            
        # DataFrameに変換し、インジケーターを計算
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True).dt.tz_convert(JST)
        
        # 指標の計算
        df = calculate_indicators(df.copy())
        
        return df
        
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
    macro_influence_score = (GLOBAL_MACRO_CONTEXT.get('fgi_proxy', 0.0) + GLOBAL_MACRO_CONTEXT.get('forex_bonus', 0.0))
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

    # 4. シグナル分析と取引実行
    analyzed_signals: List[Dict] = []
    
    # 【 Hourly Report用ログの初期化/維持 】
    if time.time() - LAST_HOURLY_NOTIFICATION_TIME > HOURLY_SCORE_REPORT_INTERVAL:
         HOURLY_SIGNAL_LOG = [] # 1時間経過したらリセット
         HOURLY_ATTEMPT_LOG = {}
         logging.info("♻️ 1時間経過したため、Hourly Report用のシグナルログをリセットします。")
         
    # 5. すべての監視銘柄を非同期で分析
    analysis_tasks = []
    for symbol in CURRENT_MONITOR_SYMBOLS:
        for tf in TARGET_TIMEFRAMES:
            analysis_tasks.append(
                asyncio.create_task(
                    # analyze_symbol内でOHLCV取得と分析が完結している
                    analyze_symbol(symbol, tf, GLOBAL_MACRO_CONTEXT) 
                )
            )

    results = await asyncio.gather(*analysis_tasks, return_exceptions=True)
    
    for result in results:
        if isinstance(result, Exception):
            # 例外処理 (OHLCV不足、APIエラーなど)
            # logging.warning(f"分析中にエラーが発生: {result}")
            # エラーメッセージからシンボルと理由を取得 (簡易的に)
            error_message = str(result)
            match = re.search(r'for (.*?)\s+\((.*?)\)', error_message)
            if match:
                 sym = match.group(1)
                 reason = "データ不足/APIエラー"
                 HOURLY_ATTEMPT_LOG[sym] = reason # エラーでスキップされた銘柄として記録
            continue
            
        if result is not None:
            analyzed_signals.append(result)
            HOURLY_SIGNAL_LOG.append(result)

    # 6. シグナルスコアでソートし、最高のシグナルを取得
    analyzed_signals.sort(key=lambda x: x['score'], reverse=True)
    
    LAST_ANALYSIS_SIGNALS = analyzed_signals # グローバル変数に保存

    if not analyzed_signals:
        logging.info("ℹ️ 分析を完了しましたが、有効なシグナルは見つかりませんでした。")
        if not IS_FIRST_MAIN_LOOP_COMPLETED:
            # 7. 初回起動完了通知 (シグナルがなくても実行)
            notification_message = format_startup_message(
                account_status=account_status,
                macro_context=GLOBAL_MACRO_CONTEXT,
                monitoring_count=len(CURRENT_MONITOR_SYMBOLS),
                current_threshold=current_threshold,
                bot_version=BOT_VERSION
            )
            await send_telegram_notification(notification_message)
            IS_FIRST_MAIN_LOOP_COMPLETED = True
            
        # 8. 1時間ごとのレポート通知 (シグナルがなくても実行)
        if time.time() - LAST_HOURLY_NOTIFICATION_TIME > HOURLY_SCORE_REPORT_INTERVAL:
            logging.info("⏳ 1時間ごとのレポート通知を実行します。")
            report_message = format_hourly_report(
                signals=HOURLY_SIGNAL_LOG,
                attempt_log=HOURLY_ATTEMPT_LOG,
                start_time=LAST_HOURLY_NOTIFICATION_TIME,
                current_threshold=current_threshold,
                bot_version=BOT_VERSION
            )
            await send_telegram_notification(report_message)
            LAST_HOURLY_NOTIFICATION_TIME = time.time()
            HOURLY_SIGNAL_LOG = []
            HOURLY_ATTEMPT_LOG = {}
        
        return # 有効なシグナルがないため終了
        
    # 7. 初回起動完了通知 (シグナルが見つかった場合でも、未完了なら実行)
    if not IS_FIRST_MAIN_LOOP_COMPLETED:
        notification_message = format_startup_message(
            account_status=account_status,
            macro_context=GLOBAL_MACRO_CONTEXT,
            monitoring_count=len(CURRENT_MONITOR_SYMBOLS),
            current_threshold=current_threshold,
            bot_version=BOT_VERSION
        )
        await send_telegram_notification(notification_message)
        IS_FIRST_MAIN_LOOP_COMPLETED = True

    best_signal = analyzed_signals[0]
    symbol_to_trade = best_signal['symbol']
    score_percent = best_signal['score'] * 100

    # 8. クールダウンとポジション保有のチェック
    can_trade_by_cooldown = time.time() - LAST_SIGNAL_TIME.get(symbol_to_trade, 0.0) > TRADE_SIGNAL_COOLDOWN
    is_position_open = any(p['symbol'] == symbol_to_trade for p in OPEN_POSITIONS)
    
    if is_position_open:
        logging.info(f"ℹ️ {symbol_to_trade}: ポジションを保有中のため、取引をスキップします。")
        trade_result = None
    elif not can_trade_by_cooldown:
        logging.info(f"ℹ️ {symbol_to_trade}: クールダウン中のため、取引をスキップします。")
        trade_result = None
    else:
        # 9. 動的閾値チェック
        if best_signal['score'] >= current_threshold:
            # 10. 取引実行（TEST_MODEでない場合）
            if not TEST_MODE:
                # 残高チェック
                if account_status['total_usdt_balance'] >= MIN_USDT_BALANCE_FOR_TRADE:
                    logging.warning(f"🚀 {symbol_to_trade} - Score: {score_percent:.2f}。取引を実行します。")
                    trade_result = await execute_trade(best_signal, account_status)
                    LAST_SIGNAL_TIME[symbol_to_trade] = time.time()
                pass
            else:
                error_message = f"残高不足 (現在: {format_usdt(account_status['total_usdt_balance'])} USDT)。新規取引に必要な額: {MIN_USDT_BALANCE_FOR_TRADE:.2f} USDT。"
                trade_result = {'status': 'error', 'error_message': error_message, 'close_status': 'skipped'}
                logging.warning(f"⚠️ {best_signal['symbol']} 取引スキップ: {error_message}")
        else:
            logging.info(f"ℹ️ {best_signal['symbol']} は閾値 {current_threshold*100:.2f} を満たしていません。取引をスキップします。")
            trade_result = None

    # 閾値を満たさないシグナルもログには記録する
    log_signal(best_signal, "取引シグナル (閾値未満/テストモード)") 

    # 11. Telegram通知
    if trade_result and trade_result.get('status') == 'ok':
        # 取引成功
        notification_message = format_telegram_message(
            signal=best_signal,
            context="取引シグナル",
            current_threshold=current_threshold,
            trade_result=trade_result
        )
        await send_telegram_notification(notification_message)
        log_signal(trade_result, "取引実行結果 (成功)")
    elif trade_result and trade_result.get('status') == 'error':
        # 取引失敗（SL/TP設定失敗、残高不足など）
        notification_message = format_telegram_message(
            signal=best_signal,
            context="取引シグナル",
            current_threshold=current_threshold,
            trade_result=trade_result # 失敗詳細を含む
        )
        await send_telegram_notification(notification_message)
        log_signal(trade_result, "取引実行結果 (失敗)")
    elif TEST_MODE and best_signal['score'] >= current_threshold:
        # テストモードでのシグナル通知
        notification_message = format_telegram_message(
            signal=best_signal,
            context="取引シグナル",
            current_threshold=current_threshold,
            trade_result={'status': 'test_mode'}
        )
        await send_telegram_notification(notification_message)
        
    # 12. 1時間ごとのレポート通知
    if time.time() - LAST_HOURLY_NOTIFICATION_TIME > HOURLY_SCORE_REPORT_INTERVAL:
        logging.info("⏳ 1時間ごとのレポート通知を実行します。")
        report_message = format_hourly_report(
            signals=HOURLY_SIGNAL_LOG,
            attempt_log=HOURLY_ATTEMPT_LOG,
            start_time=LAST_HOURLY_NOTIFICATION_TIME,
            current_threshold=current_threshold,
            bot_version=BOT_VERSION
        )
        await send_telegram_notification(report_message)
        LAST_HOURLY_NOTIFICATION_TIME = time.time()
        HOURLY_SIGNAL_LOG = []
        HOURLY_ATTEMPT_LOG = {}
        
    logging.info(f"--- 💡 BOT LOOP END. Elapsed: {time.time() - start_time:.2f}s ---")


# ====================================================================================
# CCXT CLIENT INITIALIZATION & FASTAPI SETUP
# ====================================================================================

async def load_markets_async():
    """非同期で取引所のマーケット情報をロードする"""
    global EXCHANGE_CLIENT
    try:
        if EXCHANGE_CLIENT:
            await EXCHANGE_CLIENT.load_markets()
            logging.info("✅ CCXTマーケット情報のロードに成功しました。")
        else:
            logging.critical("❌ EXCHANGE_CLIENTがNoneです。")
    except Exception as e:
        logging.critical(f"❌ CCXTマーケット情報のロード中にエラーが発生: {e}")

async def initialize_exchange_client():
    """CCXTクライアントを初期化し、マーケット情報をロードする"""
    global EXCHANGE_CLIENT, IS_CLIENT_READY
    
    if IS_CLIENT_READY:
        logging.info("ℹ️ CCXTクライアントは既に初期化済みです。")
        return
        
    try:
        # 動的な取引所クライアントのインスタンス化
        exchange_class = getattr(ccxt_async, CCXT_CLIENT_NAME.lower())
        
        # クライアントの作成 (非同期サポート版)
        EXCHANGE_CLIENT = exchange_class({
            'apiKey': API_KEY,
            'secret': SECRET_KEY,
            'enableRateLimit': True,
            'options': {
                'defaultType': 'spot', # 現物取引をデフォルトに設定
            }
        })
        
        logging.info(f"✅ CCXTクライアント ({CCXT_CLIENT_NAME.upper()} / Spot) を初期化しました。")

        # 1. マーケット情報のロード (非同期関数をawaitで呼び出す)
        logging.info("⏳ 取引所情報をロード中...")
        # asyncio.run(load_markets_async()) # ❌ この行がエラーの原因 (実行中のイベントループ内での asyncio.run は禁止)
        await load_markets_async() # ✅ 修正: await で既存のイベントループ上で実行

        IS_CLIENT_READY = True
        
    except ccxt.ExchangeNotAvailable as e:
        logging.critical(f"❌ CCXTクライアントの初期化中に致命的なエラーが発生: 取引所が利用できません ({e})", exc_info=True)
    except ccxt.AuthenticationError as e:
        logging.critical(f"❌ CCXTクライアントの初期化中に致命的なエラーが発生: 認証情報が無効です ({e})", exc_info=True)
    except Exception as e:
        logging.critical(f"❌ CCXTクライアントの初期化中に致命的なエラーが発生: {e}", exc_info=True)

async def start_background_tasks(app: FastAPI):
    """メインループと注文監視ループをバックグラウンドで開始する"""
    await initialize_exchange_client() # クライアントの初期化とマーケットロード
    
    if not IS_CLIENT_READY:
        logging.critical("🚨 CCXTクライアントの初期化に失敗したため、バックグラウンドタスクを起動できません。")
        return

    # メインBOTループ (1分ごと)
    app.state.main_loop_task = asyncio.create_task(main_loop_runner())
    
    # オープン注文監視ループ (10秒ごと)
    app.state.order_management_task = asyncio.create_task(order_management_runner())
    
    logging.info("✅ バックグラウンドタスク (メインループ & 注文管理) を起動しました。")


async def main_loop_runner():
    """main_bot_loopを定期的に実行するラッパー"""
    while True:
        try:
            await main_bot_loop()
        except Exception as e:
            logging.critical(f"🚨 致命的なエラー: メインBOTループ内で予期せぬ例外が発生しました: {e}", exc_info=True)
            # 致命的なエラー通知
            error_message = f"🚨 **致命的なエラー発生**\n\nメインBOTループの実行中にエラーが発生しました。\n\n**エラー:** <code>{str(e)[:500]}...</code>\n\n**BOTバージョン**: <code>{BOT_VERSION}</code>"
            try:
                 await send_telegram_notification(error_message)
                 logging.info(f"✅ 致命的エラー通知を送信しました (Ver: {BOT_VERSION})")
            except Exception as notify_e:
                 logging.error(f"❌ 致命的エラー通知の送信に失敗: {notify_e}")

        # 次のループまで待機
        await asyncio.sleep(LOOP_INTERVAL)


async def order_management_runner():
    """open_order_management_loopを定期的に実行するラッパー"""
    while True:
        try:
            await open_order_management_loop()
        except Exception as e:
            logging.critical(f"🚨 致命的なエラー: 注文監視ループ内で予期せぬ例外が発生しました: {e}", exc_info=True)
            # 注文管理ループの致命的なエラーは通知しない (メインループで通知されるためノイズ防止)

        # 次のループまで待機
        await asyncio.sleep(MONITOR_INTERVAL)


# FastAPIアプリケーションの初期化
app = FastAPI(title="Apex BOT API", version=BOT_VERSION)

@app.on_event("startup")
async def startup_event():
    """アプリケーション起動時に実行されるイベントハンドラ"""
    # ここで非同期タスクを開始
    asyncio.create_task(start_background_tasks(app)) # start_background_tasks はクライアントの初期化とタスク開始を処理

@app.on_event("shutdown")
async def shutdown_event():
    """アプリケーション終了時に実行されるイベントハンドラ"""
    # バックグラウンドタスクのキャンセル
    if hasattr(app.state, 'main_loop_task'):
        app.state.main_loop_task.cancel()
    if hasattr(app.state, 'order_management_task'):
        app.state.order_management_task.cancel()
        
    # CCXTクライアントのクローズ
    global EXCHANGE_CLIENT
    if EXCHANGE_CLIENT:
        try:
            await EXCHANGE_CLIENT.close()
            logging.info("✅ CCXTクライアントを正常にクローズしました。")
        except Exception as e:
            logging.error(f"❌ CCXTクライアントのクローズ中にエラーが発生: {e}")

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
    }
    
    return JSONResponse(content=status_data)

# if __name__ == "__main__":
#     # uvicorn main_render:app --host 0.0.0.0 --port $PORT を実行する
#     # Render/Docker環境では、__name__ == "__main__" ブロックは通常使用されない
#     pass
