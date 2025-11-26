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
    start_jst = datetime.fromtimestamp(start_time, JST).strftime("%H:%M:%S")
    
    # スコアでソート
    signals_sorted = sorted(signals, key=lambda x: x['score'], reverse=True)
    
    # 💡【修正】分析対象とスキップの計算ロジック
    analyzed_count = len(signals)
    skipped_count = len(attempt_log) # attempt_log はスキップされたもののみ
    total_attempted = analyzed_count + skipped_count # 総試行数
    
    best_signals = signals_sorted[:TOP_SIGNAL_COUNT]
    worst_signal = signals_sorted[-1] if signals_sorted else None
    
    # トップシグナルリストの作成
    top_signals_list = []
    for i, signal in enumerate(best_signals):
        symbol = signal['symbol'].replace('/USDT', '')
        score = signal['score'] * 100
        timeframe = signal['timeframe']
        entry = format_price_precision(signal['entry_price'])
        rr = signal['rr_ratio']
        
        # 閾値超えかどうか
        is_above_threshold = "🔥" if score / 100 >= current_threshold else "🔹"
        
        top_signals_list.append(f"  {is_above_threshold} **{symbol}** ({timeframe}) - <code>{score:.2f}</code>点 (E: {entry}, R:R 1:{rr:.2f})")
    
    # スキップされた銘柄のリスト（最大5件）
    skipped_symbols_list = []
    if attempt_log:
        for symbol, reason in list(attempt_log.items())[:5]:
            skipped_symbols_list.append(f"  - **{symbol}**: {reason}")
        if len(attempt_log) > 5:
            skipped_symbols_list.append(f"  - ...他 {len(attempt_log) - 5} 銘柄")


    message = (
        f"📈 **【Hourly Report】** - {start_jst} から {now_jst} (JST)\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - **分析対象**: <code>{total_attempted}</code> 銘柄 (成功: {analyzed_count}, スキップ: {skipped_count})\n"
        f"  - **現在閾値**: <code>{current_threshold*100:.2f}</code> 点\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n\n"
    )
    
    # 1. トップシグナル
    message += (
        f"🏆 <b>Top {len(best_signals)} Best Signals</b>\n"
        f"{'\n'.join(top_signals_list)}\n\n"
    )
    
    # 2. スキップされた銘柄
    if skipped_symbols_list:
        message += (
            f"🚫 <b>Skipped Analysis ({len(attempt_log)} / {total_attempted})</b>\n"
            f"{'\n'.join(skipped_symbols_list)}\n\n"
        )
    
    # 3. 最低スコア銘柄 (エラーで失敗したものは除く)
    if worst_signal:
        worst_symbol = worst_signal['symbol'].replace('/USDT', '')
        message += (
            f"📉 <b>Worst Signal Detected</b>\n"
            f"  - **銘柄**: <code>{worst_symbol}</code> ({worst_signal['timeframe']})\n"
            f"  - **スコア**: <code>{worst_signal['score']*100:.2f}</code> 点\n"
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
    if context == "ポジション決済" and log_data['pnl_percent'] is not None and log_data['pnl_percent'] > 0:
        log_level = logging.INFO # 利益確定はINFO
    elif context in ["取引シグナル (閾値未満/テストモード)", "ポジション監視/再設定"]:
        log_level = logging.DEBUG # 頻度の高いログはDEBUG
    elif log_data['trade_result_status'] == 'error':
        log_level = logging.ERROR # エラー時はERROR
    else:
        log_level = logging.INFO # 通常シグナルや約定完了はINFO

    try:
        # JSONシリアライズ可能でない値 (numpy.float64など) を変換
        log_output = json.dumps(_to_json_compatible(log_data), ensure_ascii=False)
        
        # ログ出力
        if log_level == logging.DEBUG:
            logging.debug(f"[{context.upper()} LOG] {log_output}")
        elif log_level == logging.INFO:
            logging.info(f"[{context.upper()} LOG] {log_output}")
        elif log_level == logging.ERROR:
            logging.error(f"[{context.upper()} LOG] {log_output}")

    except Exception as e:
        logging.error(f"❌ シグナルログの記録中にエラーが発生: {e}")
        

async def send_telegram_notification(message: str) -> None:
    """Telegramに通知を送信する"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.warning("⚠️ TelegramのトークンまたはChat IDが設定されていません。通知をスキップします。")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML", # HTML形式で整形
        "disable_web_page_preview": "true"
    }

    try:
        # requestsは同期ライブラリなので、asyncio.to_threadで非同期に実行
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(None, lambda: requests.post(url, data=payload, timeout=5))
        response.raise_for_status() # HTTPエラーが発生した場合に例外を発生させる
        logging.debug("✅ Telegram通知を送信しました。")
    except requests.exceptions.Timeout:
        logging.error("❌ Telegram通知の送信がタイムアウトしました。")
    except requests.exceptions.RequestException as e:
        # HTTP 4xx, 5xxなどのエラー
        logging.error(f"❌ Telegram通知の送信に失敗しました (Status: {response.status_code if 'response' in locals() else 'N/A'}, Error: {e})")
    except Exception as e:
        logging.error(f"❌ Telegram通知の送信中に予期せぬエラーが発生: {e}")


# ====================================================================================
# EXCHANGE UTILITIES (CCXT Async)
# ====================================================================================

async def initialize_exchange_client():
    """CCXTクライアントを初期化し、取引所の情報をロードする"""
    global EXCHANGE_CLIENT, IS_CLIENT_READY
    
    if IS_CLIENT_READY:
        return

    try:
        # CCXTクライアントの動的ロード
        exchange_class = getattr(ccxt_async, CCXT_CLIENT_NAME.lower())
        
        # クライアントのインスタンス化 (現物取引のみ)
        EXCHANGE_CLIENT = exchange_class({
            'apiKey': API_KEY,
            'secret': SECRET_KEY,
            'enableRateLimit': True, # レート制限を有効にする (重要)
            'options': {
                'defaultType': 'spot', # 現物取引をデフォルトとする
            }
        })

        # 取引所情報のロード (非同期)
        if not SKIP_MARKET_UPDATE:
            logging.info(f"⏳ {EXCHANGE_CLIENT.name} の市場情報をロード中...")
            await EXCHANGE_CLIENT.load_markets()
            logging.info(f"✅ {EXCHANGE_CLIENT.name} の市場情報をロード完了。")
        else:
            logging.warning("⚠️ SKIP_MARKET_UPDATEがTrueのため、市場情報のロードをスキップしました。")

        # 接続確認 (fetch_balanceなどで確認するのが確実だが、ここではスキップ)
        
        IS_CLIENT_READY = True
        
    except Exception as e:
        logging.critical(f"❌ 取引所クライアントの初期化に失敗しました: {e}", exc_info=True)
        IS_CLIENT_READY = False

async def fetch_account_status() -> Dict:
    """口座残高と総資産額（Equity）を取得する"""
    global EXCHANGE_CLIENT, IS_CLIENT_READY, GLOBAL_TOTAL_EQUITY
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        return {'total_usdt_balance': 0.0, 'total_equity': 0.0, 'open_positions': [], 'error': True}

    total_usdt_balance = 0.0
    total_equity = 0.0

    try:
        # 1. 残高情報の取得
        balance = await EXCHANGE_CLIENT.fetch_balance()
        
        # USDT残高 (Free + Used) を取得
        usdt_info = balance.get('USDT', {})
        total_usdt_balance = usdt_info.get('total', 0.0)
        
        # 2. 総資産額 (Equity) の計算
        # 基本的に現物取引では、各通貨の市場価値の合計が総資産額となる。
        # USDTの保有額から計算を開始
        total_equity += total_usdt_balance
        
        # USDT以外の保有資産をUSDT建てで評価
        for currency, amount in balance.get('total', {}).items():
            if currency not in ['USDT', 'USD'] and amount is not None and amount > 0.000001:
                symbol = f"{currency}/USDT"
                
                # シンボルが存在するか確認（ccxtの市場に存在しない場合はスキップ）
                if symbol not in EXCHANGE_CLIENT.markets:
                    continue
                    
                try:
                    # 最新のティッカー価格を取得
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
                symbol = f"{currency}/USDT"
                
                # シンボルが取引所に存在するか確認し、存在しない場合はハイフンなしの形式も試す
                if symbol not in EXCHANGE_CLIENT.markets:
                    if f"{currency}USDT" in EXCHANGE_CLIENT.markets:
                        symbol = f"{currency}USDT"
                    else:
                        continue
                        
                try:
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
    """出来高TOPの銘柄を取得し、監視リストを更新する"""
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.warning("⚠️ クライアント未準備のため、デフォルト銘柄リストを使用します。")
        return DEFAULT_SYMBOLS.copy()

    try:
        # スポット取引の市場から USDTペアのみをフィルタリング
        spot_markets = {s: m for s, m in EXCHANGE_CLIENT.markets.items() if m['spot'] and m['quote'] == 'USDT'}
        
        # 全てのティッカーを取得 (出来高ベースでソートするため)
        logging.info(f"⏳ {EXCHANGE_CLIENT.name} から全ティッカーを取得中...")
        tickers = await EXCHANGE_CLIENT.fetch_tickers(list(spot_markets.keys()))
        logging.info(f"✅ {len(tickers)} 件のティッカーを取得完了。")
        
        # 24時間出来高 (quote volume) でソート
        # 'quoteVolume' (または 'baseVolume') が取得できない場合もあるため、Noneチェック
        sorted_tickers = sorted(
            [t for t in tickers.values() if t and t.get('quoteVolume') is not None], 
            key=lambda t: t['quoteVolume'], 
            reverse=True
        )
        
        # TOP_SYMBOL_LIMIT に加えて、デフォルトシンボルは必ず含める
        top_symbols = set(DEFAULT_SYMBOLS)
        
        # 出来高上位の銘柄を追加
        for ticker in sorted_tickers:
            symbol = ticker['symbol']
            # すでにリストに含まれているか、および除外リストにないか確認
            if symbol not in top_symbols:
                top_symbols.add(symbol)
            
            if len(top_symbols) >= TOP_SYMBOL_LIMIT:
                break

        final_symbols = list(top_symbols)
        logging.info(f"✅ 監視銘柄リストを更新しました。合計 {len(final_symbols)} 銘柄。")
        return final_symbols

    except Exception as e:
        logging.error(f"❌ 出来高TOP銘柄の取得中にエラーが発生しました: {e}", exc_info=True)
        # 失敗した場合は、既存のリスト（またはデフォルトリスト）を返す
        return CURRENT_MONITOR_SYMBOLS or DEFAULT_SYMBOLS.copy()


async def fetch_ohlcv_and_analyze(symbol: str, timeframe: str, limit: int) -> Optional[pd.DataFrame]:
    """OHLCVデータを取得し、必要なデータ量があるか確認、インジケータを計算する"""
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.warning(f"⚠️ クライアント未準備のため、OHLCV取得をスキップします ({symbol}, {timeframe})。")
        return None

    try:
        ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(symbol, timeframe, limit=limit)
        
        if not ohlcv:
            logging.warning(f"⚠️ {symbol} ({timeframe}): OHLCVデータが空です。")
            return None

        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df.set_index('timestamp', inplace=True)
        
        # 必要なデータ量チェック (CCXTのLimitよりも実際のデータ量が少ない場合がある)
        if len(df) < limit:
            logging.warning(f"⚠️ {symbol} ({timeframe}): データ量が不足しています (取得数: {len(df)}, 必要数: {limit})。")
            return None
        
        # インジケータ計算
        df = calculate_indicators(df.copy())
        
        # NaN値を含む行を削除 (指標計算のために必要な行は確保する)
        # 念のため、SMA200に必要な行数(200行)は維持できているか確認
        if len(df.dropna()) < LONG_TERM_SMA_LENGTH:
             logging.warning(f"⚠️ {symbol} ({timeframe}): 指標計算後の有効なデータ数が不足しています (有効数: {len(df.dropna())})。")
             return None
        
        return df

    except ccxt.ExchangeError as e:
        logging.warning(f"⚠️ {symbol} ({timeframe}): 取引所OHLCVエラー ({e})。")
        raise Exception(f"Exchange OHLCV Error: {e}")
    except ccxt.NetworkError as e:
        logging.error(f"❌ {symbol} ({timeframe}): ネットワークOHLCVエラー ({e})。")
        raise Exception(f"Network OHLCV Error: {e}")
    except Exception as e:
        # このエラーは analyze_symbol でキャッチされる
        raise Exception(f"OHLCV Fetch/Indicator Calc Error for {symbol} ({timeframe}): {e}")

async def fetch_fgi_data() -> Dict:
    """外部APIからFGI (Fear & Greed Index) と為替データ(USDXなど)を取得する"""
    
    # 💡 FGIの取得 (代替プロキシを使用)
    fgi_proxy = 0.0 # -1.0 (Extreme Fear) から +1.0 (Extreme Greed) の範囲
    fgi_raw_value = 'N/A'
    
    # Fear & Greed Index のプロキシ計算 (ここでは固定値を暫定として使用)
    # 実際のプロダクトでは、外部API (Alternative.meなど) からデータ取得が必要
    try:
        fgi_response = requests.get('https://api.alternative.me/fng/?limit=1', timeout=5)
        fgi_response.raise_for_status()
        data = fgi_json = fgi_response.json().get('data', [])
        
        if data:
            fgi_value = int(data[0]['value']) # 0-100
            fgi_raw_value = data[0]['value_classification']
            
            # FGIを -1.0 ~ 1.0 に正規化
            # 50 を 0.0 に、0 を -1.0 に、100 を 1.0 に対応させる
            fgi_proxy = (fgi_value - 50) / 50.0 
        
        logging.debug(f"✅ FGIデータ取得成功: Raw={fgi_raw_value}, Proxy={fgi_proxy:.2f}")

    except Exception as e:
        logging.warning(f"⚠️ FGIデータ取得失敗: {e}。FGIプロキシを0.0に設定します。")
        fgi_proxy = 0.0
        fgi_raw_value = 'API Error'

    # 💡 為替の影響 (Forex) の取得 (USDX/DXYのプロキシ)
    forex_bonus = 0.0
    # 実際のプロダクトでは、外部APIからDXY/USDXのデータを取得し、
    # 直近の動向（例えば、DXYが下落傾向ならリスクオンとしてプラスボーナス）を評価する必要がある。
    # ここでは実装を省略し、0.0としておく。
    
    try:
        # USDXや米10年債利回りなどの取得ロジック (省略)
        # forex_bonus = ...
        pass
    except Exception as e:
        logging.warning(f"⚠️ 為替データ取得失敗: {e}。為替ボーナスを0.0に設定します。")
        forex_bonus = 0.0


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
    # pandas_taのバージョンアップにより、BBANDSのキーが 'BBL_20_2.0' -> 'BBL_20_2.0_2.0' になる場合があるため、ilocで直接指定
    if bb_data is not None and not bb_data.empty and bb_data.shape[1] >= 3:
        # インデックス 0:下限 (BBL), 1:中央 (BBM), 2:上限 (BBU), 3:幅 (BBB)
        df['BBL'] = bb_data.iloc[:, 0]
        df['BBM'] = bb_data.iloc[:, 1]
        df['BBU'] = bb_data.iloc[:, 2]
        df['BBB'] = bb_data.iloc[:, 3] # BB Width (パーセンテージ)
    else:
         df['BBL'] = np.nan
         df['BBM'] = np.nan
         df['BBU'] = np.nan
         df['BBB'] = np.nan

    # Average True Range (ATR) - ストップロス計算に使用
    df['ATR'] = ta.atr(df['high'], df['low'], df['close'], length=14)

    # On-Balance Volume (OBV)
    df['OBV'] = ta.obv(df['close'], df['volume'])
    # OBVのSMA (確認用)
    df['OBV_SMA'] = ta.sma(df['OBV'], length=20) 
    
    return df.dropna(subset=['SMA_200', 'RSI', 'MACD', 'BBL', 'ATR']) # 最低限必要な指標でNaN行を削除


def analyze_signal(df: pd.DataFrame, market_ticker: Dict, timeframe: str, macro_context: Dict) -> Optional[Dict]:
    """
    テクニカルデータフレームと市場情報に基づいて、取引シグナルを分析しスコアを計算する。
    ここではロング (買い) シグナルのみを対象とする。
    """
    
    # 1. 最新のローソク足データと市場価格を取得
    last_candle = df.iloc[-1]
    current_price = market_ticker['last']
    entry_price = current_price # 成行/指値の判断は取引実行ロジックで行うが、ここでは最新価格をエントリー候補とする

    # 2. 基本的なチェック (ロングの必要条件)
    # 価格がSMA200の上にあること（長期トレンドフィルタ）
    is_above_long_term_sma = current_price > last_candle['SMA_200']
    
    # RSIが買われすぎ水準ではないこと (70未満)
    is_not_overbought = last_candle['RSI'] < 70
    
    # MACDがシグナルラインの上にある、またはクロスしたばかりであること
    is_macd_favorable = last_candle['MACD'] > last_candle['MACDs']
    
    # 出来高が直近の平均出来高より大きいこと (出来高の増加)
    # 直近10期間の平均出来高
    avg_volume = df['volume'].iloc[-10:-1].mean()
    is_volume_increasing = last_candle['volume'] > avg_volume * 1.5 # 1.5倍以上

    # 💡 SMA50がSMA200の上にあること (中期的な上昇トレンド)
    is_mid_term_uptrend = last_candle['SMA_50'] > last_candle['SMA_200']
    
    # 💡 価格がボリンジャーバンドの下限に近づいていること (押し目買いの候補)
    # 下限 BBL から 0.5% 以内、かつ中央線 BBM よりも下にいること
    is_near_bb_low = (current_price <= last_candle['BBM']) and (current_price <= last_candle['BBL'] * 1.005)

    # 必須条件の確認 (ロングシグナルとしての最低限の要件)
    # RSIが買われすぎではないこと、BB下限に近いこと、ATRが有効であること
    if not is_not_overbought or not is_near_bb_low or last_candle['ATR'] <= 0:
        # logging.debug(f"ℹ️ {market_ticker['symbol']} ({timeframe}): ロングシグナル要件をみたしません。")
        return None 
    
    # 3. リスク・リワード (SL/TP) の計算
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
    if last_candle['SMA_50'] > last_candle['SMA_200']:
        trend_alignment_bonus_value = TREND_ALIGNMENT_BONUS
        total_score += trend_alignment_bonus_value
        
    tech_data['trend_alignment_bonus_value'] = trend_alignment_bonus_value

    # D. 価格構造/ピボット支持ボーナス (6点)
    # 価格がSMA50/SMA20のいずれかの上にいる（またはBB中央線の上）場合はボーナス
    structural_pivot_bonus = 0.0
    if current_price > last_candle['SMA_50'] or current_price > last_candle['BBM']:
        structural_pivot_bonus = STRUCTURAL_PIVOT_BONUS
        total_score += structural_pivot_bonus
        
    tech_data['structural_pivot_bonus'] = structural_pivot_bonus

    # E. MACDクロス/発散ペナルティ (25点)
    # MACDがシグナルラインの下にあり、かつヒストグラムがマイナス域で拡大している場合にペナルティ
    macd_penalty_value = 0.0
    if last_candle['MACD'] < last_candle['MACDs'] and last_candle['MACDh'] < 0:
        macd_penalty_value = MACD_CROSS_PENALTY
        total_score -= macd_penalty_value

    tech_data['macd_penalty_value'] = macd_penalty_value

    # F. RSIモメンタムボーナス (10点)
    # RSIが45以下で、かつ直近で上昇傾向にある場合にボーナス
    rsi_momentum_bonus_value = 0.0
    if last_candle['RSI'] <= RSI_MOMENTUM_LOW:
        # RSIが低いほどボーナスが増加 (線形)
        # 45で0点, 30でMAXボーナス (10点)
        rsi_range = RSI_MOMENTUM_LOW - 30.0
        if rsi_range > 0 and last_candle['RSI'] < 45:
            # 45からRSI値までの差分を正規化
            ratio = (RSI_MOMENTUM_LOW - last_candle['RSI']) / rsi_range
            rsi_momentum_bonus_value = min(ratio, 1.0) * RSI_MOMENTUM_BONUS_MAX
            total_score += rsi_momentum_bonus_value
            
    tech_data['rsi_momentum_bonus_value'] = rsi_momentum_bonus_value
    tech_data['rsi_value'] = last_candle['RSI']

    # G. OBVモメンタム/出来高確証ボーナス (5点)
    # OBVが直近のSMAよりも上にある (買い圧力が優勢)
    obv_momentum_bonus_value = 0.0
    if last_candle['OBV'] > last_candle['OBV_SMA']:
        obv_momentum_bonus_value = OBV_MOMENTUM_BONUS
        total_score += obv_momentum_bonus_value
        
    tech_data['obv_momentum_bonus_value'] = obv_momentum_bonus_value

    # H. 出来高スパイクボーナス (7点)
    volume_increase_bonus_value = 0.0
    if is_volume_increasing:
        volume_increase_bonus_value = VOLUME_INCREASE_BONUS
        total_score += volume_increase_bonus_value

    tech_data['volume_increase_bonus_value'] = volume_increase_bonus_value

    # I. 流動性ボーナス (7点) - 最新のティッカー情報から計算
    # ビッドとアスクの差 (スプレッド) が小さいほど、出来高が大きいほどボーナス
    liquidity_bonus_value = 0.0
    if market_ticker.get('ask') and market_ticker.get('bid'):
        spread = market_ticker['ask'] - market_ticker['bid']
        relative_spread = spread / current_price 
        
        # 相対スプレッドが 0.05% (0.0005) 未満ならボーナス
        if relative_spread < 0.0005:
             liquidity_bonus_value += LIQUIDITY_BONUS_MAX * 0.5
        
        # quoteVolumeが大きければさらにボーナス
        if market_ticker.get('quoteVolume') and market_ticker['quoteVolume'] > 5000000: # 500万USDT以上
             liquidity_bonus_value += LIQUIDITY_BONUS_MAX * 0.5
        
        liquidity_bonus_value = min(liquidity_bonus_value, LIQUIDITY_BONUS_MAX) # 最大値を制限
        total_score += liquidity_bonus_value

    tech_data['liquidity_bonus_value'] = liquidity_bonus_value
    
    # J. 低ボラティリティペナルティ (BB Width)
    volatility_penalty_value = 0.0
    if last_candle['BBB'] is not None and last_candle['BBB'] < VOLATILITY_BB_PENALTY_THRESHOLD * 100: # BBBはパーセンテージ
        # BB幅が狭すぎる場合、値動きが小さすぎてSL/TPが機能しないリスク
        volatility_penalty_value = -0.10 # 10点のペナルティ
        total_score += volatility_penalty_value # マイナス値なので加算
        
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

# ====================================================================================
# TRADE EXECUTION LOGIC
# ====================================================================================

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
    
    # 最小ロットはBASE_TRADE_SIZE_USDTを下回らない
    base_lot = max(min_usdt_lot, min_lot_from_equity)
    
    # スコアに基づくロット増加分の計算
    # スコアが SIGNAL_THRESHOLD_NORMAL (0.84) から DYNAMIC_LOT_SCORE_MAX (0.96) まで線形に増加
    score_min = SIGNAL_THRESHOLD_NORMAL # 0.84 (84点)
    score_max = DYNAMIC_LOT_SCORE_MAX # 0.96 (96点)
    
    lot_range = max_lot_from_equity - base_lot
    
    if score <= score_min:
        dynamic_lot_size = base_lot
    elif score >= score_max:
        dynamic_lot_size = max_lot_from_equity
    else:
        # スコア範囲内での線形補間
        ratio = (score - score_min) / (score_max - score_min)
        dynamic_lot_size = base_lot + (lot_range * ratio)
    
    # 4. USDT残高がロットサイズを上回っていることを確認
    available_usdt = account_status.get('total_usdt_balance', 0.0)
    
    if dynamic_lot_size > available_usdt:
        # 利用可能残高がロットサイズより少ない場合は、利用可能残高に調整
        # ただし、新規取引に必要な最小残高 MIN_USDT_BALANCE_FOR_TRADE は残す
        dynamic_lot_size = available_usdt - 1.0 # 1.0 USDTはバッファとして残す
        dynamic_lot_size = max(min_usdt_lot, dynamic_lot_size)
    
    # 最小ロットサイズを下回らないようにクリッピング
    return max(min_usdt_lot, dynamic_lot_size)


async def adjust_order_amount(symbol: str, lot_size_usdt: float, price: float) -> Tuple[float, float]:
    """
    指定されたUSDTロットサイズと価格から、取引所の精度に合わせて注文数量 (Base Amount) を調整する
    Returns: (調整後のBase Amount, 最終的なUSDT投入額)
    """
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY or symbol not in EXCHANGE_CLIENT.markets:
        logging.error(f"❌ 注文数量調整失敗: クライアント未準備またはシンボル ({symbol}) 不明。")
        return 0.0, 0.0

    market = EXCHANGE_CLIENT.markets[symbol]
    
    # 1. 概算の Base Amount を計算
    base_amount = lot_size_usdt / price
    
    # 2. 取引所の数量精度 (amount precision) に合わせて丸める
    amount_precision = market.get('precision', {}).get('amount')
    
    if amount_precision is not None:
        # ccxtの round_to precision メソッドを使用
        base_amount_rounded = EXCHANGE_CLIENT.amount_to_precision(symbol, base_amount)
        # floatに変換 (ccxtの戻り値はstrの場合がある)
        base_amount_rounded = float(base_amount_rounded)
        
        # 最小ロット数量 (min amount) のチェック
        min_amount = market.get('limits', {}).get('amount', {}).get('min', 0.0)
        if base_amount_rounded < min_amount:
            logging.warning(f"⚠️ {symbol}: 計算された数量 {base_amount_rounded:.4f} は最小注文数量 {min_amount:.4f} を下回ります。最小数量を使用します。")
            base_amount_rounded = min_amount
        
        # 最終的なUSDT投入額を再計算
        final_usdt_amount = base_amount_rounded * price
        
        return base_amount_rounded, final_usdt_amount
    else:
        # 精度情報がない場合はそのまま返す (リスクあり)
        logging.warning(f"⚠️ {symbol}: 数量精度情報が見つかりません。丸め処理をスキップします。")
        return base_amount, lot_size_usdt


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
        # Stop-Lossは取引所ごとに設定方法が異なるため、ccxtの unified stopLoss/takeProfit メソッドがあればそちらを使用
        # 現物取引では、通常はストップリミット注文（トリガー価格とリミット価格）を使用する
        
        # Stop Limit Order (Example for MEXC/Bybit Spot - requires custom params)
        # SLトリガー価格を stop_loss に設定し、リミット価格を stop_loss の少し下に設定する (例: 0.1%下)
        stop_price = stop_loss 
        # リミット価格はトリガー価格よりもさらに不利な価格 (急な値動きで約定を確実にするため)
        limit_price = stop_price * 0.999 # 0.1%下の価格
        
        # 数量の丸め (limit_price/stop_priceの精度も考慮すべきだが、ここではamountのみに集中)
        amount_to_sell = filled_amount

        # Stop Limit Sell Order の発注
        sl_order = await EXCHANGE_CLIENT.create_order(
            symbol=symbol,
            type='stop_limit', # または取引所独自のタイプ (例: MEXCの 'limit_maker' + 'stopLoss')
            side='sell',
            amount=amount_to_sell,
            price=limit_price, # リミット価格
            params={
                'stopPrice': stop_price, # トリガー価格
                'clientOrderId': f'SL-{uuid.uuid4()}'
            }
        )
        
        sl_order_id = sl_order['id']
        logging.info(f"✅ SL注文成功: ID={sl_order_id}, Stop Price={format_price_precision(stop_price)}, Limit Price={format_price_precision(limit_price)}")

    except Exception as e:
        # SL設定失敗はTP注文をキャンセルし、致命的なエラーとして扱う
        logging.critical(f"❌ SL注文失敗 ({symbol}): {e}。TP注文をキャンセルします。")
        try:
            await EXCHANGE_CLIENT.cancel_order(tp_order_id, symbol)
            logging.warning(f"⚠️ TP注文 (ID: {tp_order_id}) をキャンセルしました。")
        except Exception as cancel_e:
            logging.error(f"❌ TP注文のキャンセルにも失敗しました ({cancel_e})。")
            
        return {'status': 'error', 'error_message': f'SL注文失敗: {e}'}

    return {'status': 'ok', 'sl_order_id': sl_order_id, 'tp_order_id': tp_order_id}


async def close_position_immediately(symbol: str, amount: float) -> Dict:
    """
    約定に失敗した、またはSL/TP設定に失敗した不完全なポジションを成行で即時クローズする。
    Returns: {'status': 'ok', 'closed_amount': 0.0} または {'status': 'error', 'error_message': '...'}
    """
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY or amount <= 0:
        return {'status': 'skipped', 'error_message': 'スキップ: クライアント未準備または数量がゼロです。'}

    logging.warning(f"🚨 不完全ポジションの強制クローズを試みます: {symbol} (数量: {amount:.4f})")
    
    try:
        # 数量の丸め（成行注文でも精度は重要）
        # 価格情報がないため、市場情報から概算価格を取得
        # ccxtの amount_to_precision を使って丸める
        market = EXCHANGE_CLIENT.markets[symbol]
        amount = EXCHANGE_CLIENT.amount_to_precision(symbol, amount)
        amount = float(amount)

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
    global EXCHANGE_CLIENT, OPEN_POSITIONS, MIN_USDT_BALANCE_FOR_TRADE
    
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
        error_message = f"注文数量が0または最小ロットを下回ります (USDT: {final_usdt_amount:.2f} / BASE: {base_amount_to_buy:.4f})"
        logging.error(f"❌ 取引実行失敗 ({symbol}): {error_message}")
        return {'status': 'error', 'error_message': error_message, 'close_status': 'skipped'}
    
    # 3. 実行前の最終チェック
    available_usdt = account_status.get('total_usdt_balance', 0.0)
    if available_usdt < MIN_USDT_BALANCE_FOR_TRADE:
        error_message = f"残高不足 (現在: {format_usdt(available_usdt)} USDT)。新規取引に必要な額: {MIN_USDT_BALANCE_FOR_TRADE:.2f} USDT。"
        logging.warning(f"⚠️ {symbol} 取引スキップ: {error_message}")
        return {'status': 'error', 'error_message': error_message, 'close_status': 'skipped'}

    # 4. 現物指値買い注文 (IOC: Immediate-Or-Cancel) の発注
    # IOCを使用することで、即時約定しない場合は注文がキャンセルされ、意図しない指値注文の残存を防ぐ
    
    if TEST_MODE:
        return {'status': 'error', 'error_message': 'TEST_MODEのため取引をスキップ', 'close_status': 'skipped'}

    entry_order = None
    filled_amount = 0.0
    filled_usdt = 0.0 # 実際に約定したUSDT金額 (手数料除く)
    
    try:
        # 指値買い (limit) + IOC (Immediate-Or-Cancel) パラメータ
        logging.info(f"⏳ 現物指値買い注文 (IOC) を発注中: {symbol} (Qty: {base_amount_to_buy:.4f}, Price: {format_price_precision(entry_price)})")
        
        # ccxtは 'timeInForce': 'IOC' をサポートしている必要がある
        entry_order = await EXCHANGE_CLIENT.create_order(
            symbol=symbol,
            type='limit', # 指値
            side='buy',
            amount=base_amount_to_buy,
            price=entry_price,
            params={
                'timeInForce': 'IOC' # 即時約定しなかった場合はキャンセル
            }
        )
        
        # 注文結果の確認
        if entry_order and entry_order.get('status') == 'closed' and entry_order.get('filled', 0.0) > 0:
            filled_amount = entry_order['filled']
            # 約定したUSDT額 (手数料は含まない)
            filled_usdt = filled_amount * entry_order.get('price', entry_price) 
            
            logging.info(f"✅ 指値買い注文 (IOC) 成功: {symbol} - {filled_amount:.4f} 約定 (Avg Price: {format_price_precision(entry_order.get('average', entry_price))})")
            
        else:
            # 即時約定しなかったため、注文はキャンセルされたと見なす
            error_message = f"指値買い注文 (IOC) が即時約定せず、キャンセルされました。"
            logging.warning(f"⚠️ {symbol} 取引スキップ: {error_message}")
            return {'status': 'error', 'error_message': error_message, 'close_status': 'skipped'}


    # 5. SL/TP注文の設定
    if filled_amount > 0.0:
        sl_tp_result = await place_sl_tp_orders(symbol, filled_amount, stop_loss, take_profit)
        
        if sl_tp_result['status'] == 'ok':
            # 6. ポジションリストに保存
            position_id = str(uuid.uuid4())
            OPEN_POSITIONS.append({
                'id': position_id,
                'symbol': symbol,
                'entry_price': entry_order.get('average', entry_price), # 平均約定価格を優先
                'filled_amount': filled_amount,
                'filled_usdt': filled_usdt,
                'stop_loss': stop_loss,
                'take_profit': take_profit,
                'sl_order_id': sl_tp_result['sl_order_id'],
                'tp_order_id': sl_tp_result['tp_order_id'],
                'opened_at': time.time(),
            })
            
            return {
                'status': 'ok',
                'order_id': entry_order['id'],
                'filled_amount': filled_amount,
                'filled_usdt': filled_usdt,
                'sl_order_id': sl_tp_result['sl_order_id'],
                'tp_order_id': sl_tp_result['tp_order_id'],
                'position_id': position_id,
                'entry_price': entry_order.get('average', entry_price),
                'close_status': 'skipped'
            }
            
        else:
            # SL/TP設定失敗: 約定したポジションを直ちに成行で決済する
            logging.critical(f"🚨 SL/TP設定に失敗しました ({symbol})。約定数量 {filled_amount:.4f} を強制クローズします。")
            close_result = await close_position_immediately(symbol, filled_amount)
            
            return {
                'status': 'error',
                'order_id': entry_order['id'],
                'filled_amount': filled_amount,
                'filled_usdt': filled_usdt,
                'error_message': f'SL/TP設定失敗: {sl_tp_result["error_message"]}',
                'close_status': close_result['status'],
                'closed_amount': close_result.get('closed_amount', 0.0),
                'close_error_message': close_result.get('error_message'),
            }
    
    # ここに到達した場合 (filled_amountが0だが、エラーではない場合: IOCで約定ゼロなど)
    error_message = f"指値買い注文 (IOC) が約定数量ゼロで終了しました。"
    logging.warning(f"⚠️ {symbol} 取引スキップ: {error_message}")
    return {'status': 'error', 'error_message': error_message, 'close_status': 'skipped'}
    
    
async def open_order_management_loop():
    """オープン注文（ポジション）の状態を監視し、決済されたものを処理する"""
    global OPEN_POSITIONS, GLOBAL_TOTAL_EQUITY, EXCHANGE_CLIENT
    
    if not OPEN_POSITIONS:
        return
    
    logging.debug(f"ℹ️ オープン注文監視ループ開始。監視中のポジション: {len(OPEN_POSITIONS)}")
    
    positions_to_remove_ids = []

    for position in OPEN_POSITIONS:
        symbol = position['symbol']
        sl_order_id = position['sl_order_id']
        tp_order_id = position['tp_order_id']
        
        is_closed = False
        exit_type = '不明'
        
        # 1. SL/TP注文のステータスを取得
        # SL注文の状態確認
        sl_status = await check_order_status(sl_order_id, symbol)
        is_sl_open = sl_status is not None and sl_status.get('status') in ['open', 'partial']
        is_sl_filled = sl_status is not None and sl_status.get('status') == 'closed' and sl_status.get('filled', 0.0) > 0.0
        
        # TP注文の状態確認
        tp_status = await check_order_status(tp_order_id, symbol)
        is_tp_open = tp_status is not None and tp_status.get('status') in ['open', 'partial']
        is_tp_filled = tp_status is not None and tp_status.get('status') == 'closed' and tp_status.get('filled', 0.0) > 0.0

        # 2. 決済の判断
        if is_sl_filled:
            is_closed = True
            exit_type = 'SL約定'
            logging.info(f"🛑 {symbol}: SL注文 (ID: {sl_order_id}) が約定しました。")
            
        elif is_tp_filled:
            is_closed = True
            exit_type = 'TP約定'
            logging.info(f"🛑 {symbol}: TP注文 (ID: {tp_order_id}) が約定しました。")
            
        elif not is_sl_open and not is_tp_open:
            # 💡 どちらの注文もオープン状態ではない場合 (手動キャンセル/約定の可能性)
            # ポジションの残高を確認
            balance = await EXCHANGE_CLIENT.fetch_balance()
            base_currency = symbol.split('/')[0]
            base_amount = balance.get(base_currency, {}).get('total', 0.0)
            
            if base_amount < position['filled_amount'] * 0.1: # 90%以上売却済みと見なす
                is_closed = True
                exit_type = '手動決済/外部決済'
                logging.warning(f"⚠️ {symbol}: SL/TP注文がキャンセル/約定済みですが、ポジション残高がほとんどありません。手動決済と見なします。")
            else:
                # 💡 SLまたはTPの注文が片方または両方欠けている場合に、残っている注文をキャンセルし、SL/TP注文を再設定するロジック
                # (V19.0.53で追加)
                if not is_sl_open and is_tp_open:
                    # SLが消えているがTPが残っている
                    logging.warning(f"⚠️ {symbol}: SL注文 (ID: {sl_order_id}) が欠落しています。TP注文をキャンセルし、再設定を試みます。")
                elif is_sl_open and not is_tp_open:
                    # TPが消えているがSLが残っている
                    logging.warning(f"⚠️ {symbol}: TP注文 (ID: {tp_order_id}) が欠落しています。SL注文をキャンセルし、再設定を試みます。")
                elif not is_sl_open and not is_tp_open and base_amount >= position['filled_amount'] * 0.9:
                     # 両方欠落しており、ポジションは残っている
                    logging.warning(f"⚠️ {symbol}: SL/TP注文が両方とも欠落しています。ポジション残高が {base_amount:.4f} 残っています。再設定を試みます。")
                
                # 再設定ロジックの実行
                if not is_closed:
                    try:
                        # 既存のTP/SL注文をキャンセル (念のため)
                        if is_sl_open:
                             await EXCHANGE_CLIENT.cancel_order(sl_order_id, symbol)
                        if is_tp_open:
                             await EXCHANGE_CLIENT.cancel_order(tp_order_id, symbol)
                        
                        # SL/TPを再設定
                        re_place_result = await place_sl_tp_orders(
                            symbol, 
                            position['filled_amount'], 
                            position['stop_loss'], 
                            position['take_profit']
                        )
                        
                        if re_place_result['status'] == 'ok':
                            position['sl_order_id'] = re_place_result['sl_order_id']
                            position['tp_order_id'] = re_place_result['tp_order_id']
                            logging.info(f"✅ {symbol}: SL/TP注文を再設定しました。")
                            log_signal(position, "ポジション監視/再設定")
                        else:
                            logging.error(f"❌ {symbol}: SL/TP注文の再設定に失敗: {re_place_result['error_message']}")

                    except Exception as e:
                        logging.error(f"❌ {symbol}: SL/TP注文の再設定処理中にエラーが発生: {e}")
                        
                # 再設定後もポジションがクローズされていないので、ループは継続
                
        else:
            # 注文は引き続きオープン中
            logging.debug(f"ℹ️ {symbol}: ポジションは引き続きオープン中 (SL: {is_sl_open}, TP: {is_tp_open})")
            pass

        
        if is_closed:
            positions_to_remove_ids.append(position['id'])
            
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
                signal_for_log = {'symbol': symbol, 'timeframe': '1h', 'entry_price': entry_price} # PnL計算に必要な情報を渡す
                notification_message = format_telegram_message(signal_for_log, "ポジション決済", SIGNAL_THRESHOLD_NORMAL, closed_result)
                await send_telegram_notification(notification_message)
                log_signal(closed_result, "ポジション決済")

            except Exception as e:
                logging.error(f"❌ 決済処理後のPnL計算/通知中にエラーが発生 ({symbol}): {e}", exc_info=True)
                pass # 決済通知失敗しても、ポジションは削除する


    # 3. 決済済みポジションをリストから削除
    OPEN_POSITIONS = [p for p in OPEN_POSITIONS if p['id'] not in positions_to_remove_ids]
    
    if positions_to_remove_ids:
        logging.info(f"✅ 監視中のポジションから {len(positions_to_remove_ids)} 件を削除しました。残存: {len(OPEN_POSITIONS)}")

    
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

    # 4. 全ての監視銘柄の最新ティッカーを取得 (並行処理の効率化のため)
    market_tickers = {}
    try:
        tickers = await EXCHANGE_CLIENT.fetch_tickers(symbols=CURRENT_MONITOR_SYMBOLS)
        market_tickers = {k: v for k, v in tickers.items() if v}
    except Exception as e:
        logging.error(f"❌ ティッカー情報の取得に失敗: {e}")
        # ティッカー取得に失敗した場合、その後の分析はスキップ

    # 5. 全ての監視銘柄に対して分析を実行
    analysis_tasks = []
    current_analysis_signals = []
    symbols_to_analyze = CURRENT_MONITOR_SYMBOLS.copy()
    
    # 1時間ごとのログをリセット
    HOURLY_SIGNAL_LOG = [] 
    HOURLY_ATTEMPT_LOG = {}

    for symbol in symbols_to_analyze:
        # クールダウンチェック (取引シグナル通知から2時間以内はスキップ)
        if symbol in LAST_SIGNAL_TIME and (time.time() - LAST_SIGNAL_TIME[symbol] < TRADE_SIGNAL_COOLDOWN):
            HOURLY_ATTEMPT_LOG[symbol] = "クールダウン期間中"
            continue

        # ポジション保有中はスキップ
        if any(p['symbol'] == symbol for p in OPEN_POSITIONS):
            HOURLY_ATTEMPT_LOG[symbol] = "ポジション保有中"
            continue
            
        # ティッカー情報がない場合はスキップ (エラーログは既に出ているはず)
        if symbol not in market_tickers:
             HOURLY_ATTEMPT_LOG[symbol] = "ティッカー情報なし"
             continue

        # 各タイムフレームの分析タスクを作成
        for timeframe in TARGET_TIMEFRAMES:
            limit = REQUIRED_OHLCV_LIMITS.get(timeframe, 500)
            # analyze_and_score を非同期で実行するタスクを作成
            analysis_tasks.append(
                asyncio.create_task(
                    analyze_and_score_symbol(symbol, timeframe, limit, market_tickers.get(symbol), GLOBAL_MACRO_CONTEXT)
                )
            )

    # 6. 並行で分析を実行し、結果を待機
    if analysis_tasks:
        logging.info(f"⏳ {len(analysis_tasks)} 件の分析タスク ({len(symbols_to_analyze)} 銘柄) を並行で実行します...")
        results = await asyncio.gather(*analysis_tasks, return_exceptions=True)
        logging.info(f"✅ 全ての分析タスクが完了しました。")

        for result in results:
            if isinstance(result, Dict) and result.get('score', 0.0) > 0.0:
                current_analysis_signals.append(result)
                HOURLY_SIGNAL_LOG.append(result)
            elif isinstance(result, Exception):
                logging.error(f"❌ 分析タスクでエラーが発生: {result}")
            # Noneが返された場合は、シグナルなしとして無視
            
    LAST_ANALYSIS_SIGNALS = current_analysis_signals
    
    # 7. スコアの高いシグナルをフィルタリングし、ソート
    tradeable_signals = [s for s in current_analysis_signals if s['score'] >= current_threshold]
    tradeable_signals_sorted = sorted(tradeable_signals, key=lambda s: s['score'], reverse=True)
    
    # 8. 初回起動完了通知 (一度だけ実行)
    if not IS_FIRST_MAIN_LOOP_COMPLETED:
        notification_message = format_startup_message(account_status, GLOBAL_MACRO_CONTEXT, len(CURRENT_MONITOR_SYMBOLS), current_threshold, BOT_VERSION)
        await send_telegram_notification(notification_message)
        IS_FIRST_MAIN_LOOP_COMPLETED = True
        
    # 9. Hourly Reportの通知
    if time.time() - LAST_HOURLY_NOTIFICATION_TIME > HOURLY_SCORE_REPORT_INTERVAL:
        if HOURLY_SIGNAL_LOG:
            report_message = format_hourly_report(HOURLY_SIGNAL_LOG, HOURLY_ATTEMPT_LOG, LAST_HOURLY_NOTIFICATION_TIME, current_threshold, BOT_VERSION)
            await send_telegram_notification(report_message)
        LAST_HOURLY_NOTIFICATION_TIME = time.time()
        
    # 10. 最もスコアの高いシグナルで取引を実行
    if tradeable_signals_sorted and not TEST_MODE:
        best_signal = tradeable_signals_sorted[0]
        
        # USDT残高チェック
        if account_status.get('total_usdt_balance', 0.0) >= MIN_USDT_BALANCE_FOR_TRADE:
            logging.info(f"🚀 最適シグナル検出: {best_signal['symbol']} ({best_signal['timeframe']}) - Score: {best_signal['score']*100:.2f}。取引を実行します。")
            trade_result = await execute_trade(best_signal, account_status)
            
            # 取引が成功した場合、その銘柄のクールダウン時間を更新
            if trade_result['status'] == 'ok':
                LAST_SIGNAL_TIME[best_signal['symbol']] = time.time()
            
            # シグナルと取引結果をログに記録
            log_signal({**best_signal, **trade_result}, "取引シグナル (実行結果)")
            
        else:
            error_message = f"残高不足 (現在: {format_usdt(account_status['total_usdt_balance'])} USDT)。新規取引に必要な額: {MIN_USDT_BALANCE_FOR_TRADE:.2f} USDT。"
            trade_result = {'status': 'error', 'error_message': error_message, 'close_status': 'skipped'}
            logging.warning(f"⚠️ {best_signal['symbol']} 取引スキップ: {error_message}")
            # 閾値を満たさないシグナルもログには記録する
            log_signal(best_signal, "取引シグナル (閾値未満/テストモード)")
    
    else:
        # 閾値を満たすシグナルがない場合、またはテストモードの場合
        trade_result = None
        # 最もスコアの高いシグナルがあればログに記録
        if current_analysis_signals:
            top_signal_for_log = sorted(current_analysis_signals, key=lambda s: s['score'], reverse=True)[0]
            log_signal(top_signal_for_log, "取引シグナル (閾値未満/テストモード)")

    # 11. Telegram通知
    if trade_result and (trade_result.get('status') == 'ok' or 'error' in trade_result.get('error_message', '')):
        # 取引成功 または 取引失敗 (エラーメッセージに 'error' が含まれる場合) のみ通知
        notification_message = format_telegram_message(best_signal, "取引シグナル", current_threshold, trade_result)
        await send_telegram_notification(notification_message)
        
    logging.info(f"--- 🏁 BOT LOOP END (Duration: {time.time() - start_time:.2f}s) ---")
    

async def analyze_and_score_symbol(symbol: str, timeframe: str, limit: int, market_ticker: Optional[Dict], macro_context: Dict) -> Optional[Dict]:
    """
    個別の銘柄・タイムフレームでOHLCVを取得し、分析・スコアリングを行う。
    エラー発生時はNoneを返す。
    """
    if market_ticker is None:
        # ティッカー情報の取得に失敗している
        return None 
        
    try:
        # OHLCV取得と指標計算
        df = await fetch_ohlcv_and_analyze(symbol, timeframe, limit)
        
        if df is None:
            # データ不足などで分析失敗
            HOURLY_ATTEMPT_LOG[symbol] = f"{timeframe}データ不足"
            return None
            
        # スコアリング
        signal = analyze_signal(df, market_ticker, timeframe, macro_context)
        
        if signal is None:
            # シグナル無し
            return None
        
        return signal

    except Exception as e:
        # OHLCV取得や指標計算、分析中のエラー
        logging.error(f"❌ {symbol} ({timeframe}) の分析中にエラーが発生: {e}")
        HOURLY_ATTEMPT_LOG[symbol] = f"{timeframe}分析エラー"
        return None

async def position_management_loop():
    """
    オープン注文監視ループ。メインループとは独立して動作する。
    """
    while True:
        try:
            await open_order_management_loop()
        except Exception as e:
            logging.critical(f"🚨 ポジション監視ループで致命的なエラーが発生: {e}", exc_info=True)

        await asyncio.sleep(MONITOR_INTERVAL)


# ====================================================================================
# STARTUP & FASTAPI INTEGRATION
# ====================================================================================

# FastAPIアプリケーションの初期化
app = FastAPI()

async def startup_event():
    """アプリケーション起動時に実行されるタスク"""
    global IS_CLIENT_READY, LAST_HOURLY_NOTIFICATION_TIME
    
    logging.info("🤖 Apex BOT 起動処理を開始します...")
    
    # クライアントの初期化
    await initialize_exchange_client()
    
    if not IS_CLIENT_READY:
        logging.critical("❌ CCXTクライアントの初期化に失敗しました。BOTは機能しません。")
        return
        
    # 初回監視銘柄リストの取得
    global CURRENT_MONITOR_SYMBOLS
    CURRENT_MONITOR_SYMBOLS = await fetch_top_symbols()
    
    # 初回FGIデータ取得
    global GLOBAL_MACRO_CONTEXT
    GLOBAL_MACRO_CONTEXT = await fetch_fgi_data()
    
    # 初回口座ステータス取得
    await fetch_account_status()
    
    # Hourly Reportの基準時刻を現在に設定
    LAST_HOURLY_NOTIFICATION_TIME = time.time()
    
    # メインループとポジション管理ループを非同期で開始
    asyncio.create_task(main_loop_runner())
    asyncio.create_task(position_management_loop())
    
    logging.info("✅ Apex BOT 起動処理が完了しました。")

app.add_event_handler("startup", startup_event)


async def main_loop_runner():
    """
    メインBOTループを定期的に実行するラッパー関数
    """
    # 最初のループが起動イベント内で完了する前に開始されるのを防ぐため、短時間待機
    # await asyncio.sleep(1) 
    
    while True:
        try:
            await main_bot_loop()
        except Exception as e:
            logging.critical(f"🚨 メインBOTループで致命的なエラーが発生: {e}", exc_info=True)
            
            # 致命的エラー発生時にTelegramに通知
            error_message = f"🚨 **致命的なエラー発生**\n\nメインBOTループの実行中にエラーが発生しました。\n\n**エラー:** <code>{str(e)[:500]}...</code>\n\n**BOTバージョン**: <code>{BOT_VERSION}</code>"
            try:
                 await send_telegram_notification(error_message)
                 logging.info(f"✅ 致命的エラー通知を送信しました (Ver: {BOT_VERSION})")
            except Exception as notify_e:
                 logging.error(f"❌ 致命的エラー通知の送信に失敗: {notify_e}")

        # 次のループまで待機
        await asyncio.sleep(LOOP_INTERVAL)


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
        "last_hourly_report_time_utc": datetime.fromtimestamp(LAST_HOURLY_NOTIFICATION_TIME, timezone.utc).isoformat() if LAST_HOURLY_NOTIFICATION_TIME else "N/A",
        "hourly_signal_log_count": len(HOURLY_SIGNAL_LOG),
        "last_analysis_signals_count": len(LAST_ANALYSIS_SIGNALS),
        "open_positions_summary": [
            {
                "symbol": p['symbol'], 
                "entry_price": p['entry_price'], 
                "filled_amount": p['filled_amount'],
                "opened_at_jst": datetime.fromtimestamp(p['opened_at'], JST).strftime("%Y/%m/%d %H:%M:%S")
            } for p in OPEN_POSITIONS
        ],
    }
    
    return JSONResponse(content=_to_json_compatible(status_data))


# スクリプトを直接実行する場合
if __name__ == "__main__":
    # uvicorn.run(app, host="0.0.0.0", port=8000)
    # 開発環境向けの設定 (リロード有効)
    uvicorn.run(
        "main_render:app", 
        host="0.0.0.0", 
        port=int(os.getenv("PORT", 8000)), 
        log_level="info", 
        reload=True if os.getenv("ENV") == "development" else False
    )
