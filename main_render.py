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
        f"\n"
        f"🟢 **ベストスコア銘柄 (Top)**\n"
        f"  - **銘柄**: <b>{best_signal['symbol']}</b> ({best_signal['timeframe']})\n"
        f"  - **スコア**: <code>{best_signal['score'] * 100:.2f} / 100</code>\n"
        f"  - **推定勝率**: <code>{get_estimated_win_rate(best_signal['score'])}</code>\n"
        f"  - **指値 (Entry)**: <code>{format_price_precision(best_signal['entry_price'])}</code>\n"
        f"  - **SL/TP**: <code>{format_price_precision(best_signal['stop_loss'])}</code> / <code>{format_price_precision(best_signal['take_profit'])}</code>\n"
    )

    # ワーストシグナル (有効シグナルの中で最低スコア)
    worst_signal = signals_sorted[-1]
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
        logging.warning(f"📉 {log_data['context']} - {log_data['symbol']} ({log_data['pnl_percent']:+.2f}%) - Log Data: {json.dumps(log_data, ensure_ascii=False)}")
    else:
        # シグナル、成功取引、利益決済はINFO
        logging.info(f"📊 {log_data['context']} - {log_data['symbol']} - Log Data: {json.dumps(log_data, ensure_ascii=False)}")

# ====================================================================================
# CCXT WRAPPER FUNCTIONS 
# ====================================================================================

async def initialize_exchange_client():
    """CCXTクライアントを初期化する"""
    global EXCHANGE_CLIENT, IS_CLIENT_READY
    
    exchange_class = getattr(ccxt_async, CCXT_CLIENT_NAME.lower())

    if not API_KEY or not SECRET_KEY:
        logging.critical(f"❌ APIキーまたはシークレットキーが設定されていません。({CCXT_CLIENT_NAME.upper()}_API_KEY / {CCXT_CLIENT_NAME.upper()}_SECRET)")
        sys.exit(1)

    try:
        # CCXTクライアントのインスタンス化
        EXCHANGE_CLIENT = exchange_class({
            'apiKey': API_KEY,
            'secret': SECRET_KEY,
            'enableRateLimit': True, # レート制限の自動処理を有効にする
            'options': {
                'defaultType': 'spot', # 現物取引をデフォルトとする
            },
        })
        
        # ロードマーケットは同期関数として実行可能
        await EXCHANGE_CLIENT.load_markets()
        
        # テスト接続
        await EXCHANGE_CLIENT.fetch_balance() 
        
        IS_CLIENT_READY = True
        logging.info(f"✅ CCXTクライアント ({EXCHANGE_CLIENT.name}) が正常に初期化されました。")
        
    except Exception as e:
        logging.critical(f"❌ CCXTクライアントの初期化または接続に失敗: {e}", exc_info=True)
        sys.exit(1)

async def fetch_account_status() -> Dict:
    """口座残高と総資産額を取得する"""
    global EXCHANGE_CLIENT, GLOBAL_TOTAL_EQUITY
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        return {'total_usdt_balance': 0.0, 'total_equity': 0.0, 'open_positions': [], 'error': True}
        
    try:
        balance = await EXCHANGE_CLIENT.fetch_balance()
        
        # 1. 総USDT残高の計算 (フリー+使用中)
        total_usdt_balance = balance.get('total', {}).get('USDT', 0.0)
        
        # 2. 総資産額 (Equity) の推定: USDT総額を暫定的にEquityとする (現物のみのため)
        # 厳密には他のコインのUSDT評価額も含める必要があるが、ccxtの仕様に依存するためUSDTのみをベースに計算
        # 暫定的に total_usdt_balance を Equity とする
        total_equity = total_usdt_balance 
        
        # 3. 他の保有資産をUSDT建てで評価し、Equityに加算
        for currency, amount in balance.get('total', {}).items():
            # USDT/USD以外の通貨で、残高がごくわずかでないもの
            if currency not in ['USDT', 'USD'] and amount is not None and amount > 0.000001: 
                try:
                    # その通貨のUSDT建てシンボルを構成
                    symbol = f"{currency}/USDT" 
                    # シンボルが取引所に存在するか確認し、存在しない場合はハイフンなしの形式も試す
                    if symbol not in EXCHANGE_CLIENT.markets:
                        # 例: BTCUSDT 形式
                        if f"{currency}USDT" in EXCHANGE_CLIENT.markets:
                            symbol = f"{currency}USDT"
                        else:
                            continue # 取引できないシンボルはスキップ
                            
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
                    symbol = f"{currency}/USDT" # シンボルが取引所に存在するか確認し、存在しない場合はハイフンなしの形式も試す
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
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY or SKIP_MARKET_UPDATE:
        logging.info("ℹ️ CCXTクライアント未準備またはマーケットアップデートがスキップされています。デフォルトシンボルを使用します。")
        return DEFAULT_SYMBOLS
        
    try:
        # ★ CCXTのティッカーフェッチを使用して、出来高の高いシンボルを取得
        tickers = await EXCHANGE_CLIENT.fetch_tickers()
        
        # USDT建ての現物取引ペアに限定
        usdt_spot_tickers = {
            symbol: data for symbol, data in tickers.items() 
            if symbol.endswith('/USDT') and data and data.get('quoteVolume') is not None
            and symbol in EXCHANGE_CLIENT.markets and EXCHANGE_CLIENT.markets[symbol].get('spot')
        }

        # quoteVolume (USDTでの出来高) で降順ソート
        sorted_tickers = sorted(
            usdt_spot_tickers.items(), 
            key=lambda item: item[1]['quoteVolume'] or 0, 
            reverse=True
        )
        
        # TOP Nを取得
        top_symbols = [symbol for symbol, _ in sorted_tickers[:TOP_SYMBOL_LIMIT]]
        
        # デフォルトシンボルに含まれていて、TOP_SYMBOL_LIMIT に入らなかったものも含める
        final_symbols = list(set(top_symbols + DEFAULT_SYMBOLS))
        
        # 最終的なリストをソートして返す
        final_symbols.sort()
        
        logging.info(f"✅ 出来高TOP {TOP_SYMBOL_LIMIT} 銘柄の取得に成功しました。監視銘柄数: {len(final_symbols)}")
        return final_symbols
        
    except Exception as e:
        logging.error(f"❌ 出来高TOP銘柄の取得中にエラーが発生: {e}", exc_info=True)
        logging.info("ℹ️ エラーが発生したため、デフォルトシンボルを使用します。")
        return DEFAULT_SYMBOLS

async def fetch_fgi_data() -> Dict:
    """Fear & Greed Index (FGI) データと外部マクロ要因を取得する"""
    # FGI (恐怖・貪欲指数) のみを取得するプロキシとして実装
    
    fgi_raw_value = 'N/A'
    fgi_proxy = 0.0
    forex_bonus = 0.0

async def fetch_ohlcv_with_retry(symbol: str, timeframe: str, limit: int = 500, retries: int = 3) -> pd.DataFrame:
    """ OHLCVデータの取得をエラー時に再試行するラッパー関数 """
    global EXCHANGE_CLIENT
    
    for attempt in range(retries):
    try:
    # リトライ機能付きの新しいOHLCVデータ取得処理
        df = await fetch_ohlcv_with_retry(
            symbol=symbol, 
            timeframe=tf, 
            limit=limit,
            retries=3 # 必要に応じてリトライ回数を変更
        )
    # df は既に DataFrame に変換されています
        except Exception as e:
            logging.warning(f"⚠️ {symbol}/{timeframe} - OHLCV取得試行 {attempt + 1}/{retries} 失敗: {e}")
            if attempt < retries - 1:
                # 最後の試行でなければ、ランダムな時間待機
                await asyncio.sleep(random.randint(2, 5)) # 2〜5秒ランダムに待機
            else:
                # 最終試行も失敗
                raise Exception(f"OHLCV Fetch Error: {e}") # 呼び出し元にエラーを再スロー (analyze_symbolでキャッチされる)
    
    # 外部APIからFGIを取得するロジック（実際には、ここでAPIコールを行う）
    try:
        # 例: Alternative.meのFGI APIを使用
        response = requests.get("https://api.alternative.me/fng/?limit=1")
        response.raise_for_status() # HTTPエラーをチェック
        data = response.json()
        
        if data and data.get('data'):
            latest = data['data'][0]
            fgi_value = int(latest['value'])
            fgi_raw_value = f"{latest['value_classification']} ({fgi_value})"
            
            # FGIを正規化 (0:Extreme Fear -> 100:Extreme Greed)
            normalized_fgi = (fgi_value - 50) / 50.0 
            
            # 恐怖・貪欲の度合いを -0.5 から +0.5 の範囲でプロキシとして使用
            fgi_proxy = normalized_fgi * FGI_PROXY_BONUS_MAX / 0.5 
            
            # 実際のFGIボーナスは、スコアリング時に fgi_proxy + forex_bonus を使用する
            
            # TODO: 外部マクロ要因 (例: DXY、VIXなど) を取得し、forex_bonusに加算するロジックをここに追加
            
            logging.info(f"✅ FGIデータ取得成功: {fgi_raw_value}, Proxy Influence: {fgi_proxy:.4f}")
            
    except Exception as e:
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
    if bb_data is not None and not bb_data.empty and 'BBL_20_2.0_2.0' in bb_data.columns:
        # 💡 【BBANDSキーの修正】 Key 'BBL_20_2.0' not found エラーに対応
        df['BBL'] = bb_data['BBL_20_2.0_2.0'] 
        df['BBM'] = bb_data['BBM_20_2.0_2.0'] 
        df['BBU'] = bb_data['BBU_20_2.0_2.0'] 
        df['BBB'] = bb_data['BBB_20_2.0_2.0'] # Bandwidth
    else:
        # 計算失敗時のフォールバック (特にBBANDSのBandwidthはATR計算で使用)
        df['BBL'] = np.nan
        df['BBM'] = np.nan
        df['BBU'] = np.nan
        df['BBB'] = np.nan
        
    # ATR (Average True Range) - ストップロス計算に使用
    df['ATR'] = ta.atr(df['high'], df['low'], df['close'], length=14)

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
    
    # 1. テクニカル指標の計算結果の確認
    # calculate_indicators が NaN を drop しているため、有効なデータ数をチェック
    # SMA200の計算には200本必要だが、計算後のデータが10本未満はリスク計算不可
    if df.shape[0] < 10: 
        raise Exception('Data insufficient for ATR/SL/TP calculation')
        
    last_candle = df.iloc[-1]
    
    # 2. 初期チェック (ロングシグナルの基本的な条件)
    # 条件A: 価格が20期間ボリンジャーバンドのミッドバンド (BBM) の上にある
    is_above_mid_band = last_candle['close'] > last_candle['BBM']
    
    # 条件B: 短期的なモメンタムの確認 (RSI > 50)
    is_rsi_above_50 = last_candle['RSI'] > 50
    
    # 条件C: 長期トレンドフィルタ (価格がSMA200の上にある)
    is_above_long_term_sma = last_candle['close'] > last_candle['SMA_200']
    
    # ★ シグナルの基本条件を緩和し、より多くの候補をスコアリングに乗せる (最低条件)
    # 価格がSMA200の上、またはRSIが50より上、のいずれかを満たすこと。
    if not (is_above_long_term_sma or is_rsi_above_50):
        # logging.debug(f"ℹ️ {symbol} ({timeframe}): 基本シグナル条件（SMA200またはRSI50）を満たしません。")
        return None # スコアリング対象外

    # 3. リスク/リワード (RR) の計算とエントリー価格の設定
    current_price = market_ticker['last']
    
    # エントリー価格: 現在価格または直前のローソク足の終値のどちらか高い方 (より安全なエントリー)
    # ただし、シグナルはロングなので、指値価格は現在価格か、少し下が良い。ここでは直前の終値を使用。
    entry_price = last_candle['close']
    
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
    # 現在の価格が過去のPivot Lowまたは直近のサポートレベルの上にあることを確認
    structural_pivot_bonus = 0.0
    # 簡単なチェック: 直近3本のローソク足の安値よりも、SLが十分低いこと (=価格がサポートされていること)
    min_low_3_bars = df['low'].iloc[-4:-1].min() if df.shape[0] >= 4 else df['low'].iloc[-1]
    
    # SLが直近の安値の最小値よりもさらに下にある（SLが妥当な位置にある）
    if stop_loss < min_low_3_bars: 
        structural_pivot_bonus = STRUCTURAL_PIVOT_BONUS
        total_score += structural_pivot_bonus
    tech_data['structural_pivot_bonus'] = structural_pivot_bonus

    # E. MACD クロス/発散ペナルティ (25点)
    macd_penalty_value = 0.0
    # MACDヒストグラムがマイナスであり、かつMACDラインがシグナルラインを下回っている (不利なクロス)
    if last_candle['MACDh'] < 0 and last_candle['MACD'] < last_candle['MACDs']:
        macd_penalty_value = MACD_CROSS_PENALTY
        total_score -= macd_penalty_value
    tech_data['macd_penalty_value'] = macd_penalty_value

    # F. RSI モメンタムボーナス (10点)
    rsi_momentum_bonus_value = 0.0
    rsi = last_candle['RSI']
    tech_data['rsi_value'] = rsi
    # RSI 50で0点、70でRSI_MOMENTUM_BONUS_MAX (0.10)
    # RSI 50から70の間で線形にボーナスを増加させる
    if rsi > 50.0:
        rsi_momentum_bonus_value = RSI_MOMENTUM_BONUS_MAX * min((rsi - 50.0) / 20.0, 1.0)
        total_score += rsi_momentum_bonus_value
    tech_data['rsi_momentum_bonus_value'] = rsi_momentum_bonus_value

    # G. OBV Momentum Bonus (OBVがSMAを上抜けている) (5点)
    obv_momentum_bonus_value = 0.0
    # 直近でOBVがOBV_SMAを上抜けしたこと
    if last_candle['OBV'] > last_candle['OBV_SMA'] and df['OBV'].iloc[-2] <= df['OBV_SMA'].iloc[-2]:
        obv_momentum_bonus_value = OBV_MOMENTUM_BONUS
        total_score += obv_momentum_bonus_value
    tech_data['obv_momentum_bonus_value'] = obv_momentum_bonus_value
    
    # H. Volume Spike Bonus (7点)
    volume_increase_bonus_value = 0.0
    if 'Volume_SMA20' in df.columns and last_candle['Volume_SMA20'] > 0 and last_candle['volume'] > last_candle['Volume_SMA20'] * 1.5: # 出来高が平均の1.5倍
        volume_increase_bonus_value = VOLUME_INCREASE_BONUS
        total_score += volume_increase_bonus_value
    tech_data['volume_increase_bonus_value'] = volume_increase_bonus_value
    
    # I. Volatility Penalty (ボリンジャーバンド幅が狭すぎる場合)
    volatility_penalty_value = 0.0
    bb_width_percent = last_candle['BBB']
    if bb_width_percent < VOLATILITY_BB_PENALTY_THRESHOLD: # BB幅が1%未満
        volatility_penalty_value = -0.05 # 5点ペナルティ
        total_score += volatility_penalty_value
    tech_data['volatility_penalty_value'] = volatility_penalty_value
    
    # J. 流動性ボーナス (7点)
    # 取引所の板情報など、よりリアルタイムな流動性情報があればここで加算。
    # ここでは便宜的に、現在の価格変動率（例: ATRの相対的な大きさ）に基づいて流動性を推測し、ATRが小さいほど流動性が高いと見なす
    liquidity_bonus_value = LIQUIDITY_BONUS_MAX # 最大値をデフォルトとする
    # より多くの情報があれば、この部分を洗練させる
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
    
    # 3. 動的ロットの計算範囲 (総資産のX%からY%)
    min_lot_from_equity = total_equity * DYNAMIC_LOT_MIN_PERCENT
    max_lot_from_equity = total_equity * DYNAMIC_LOT_MAX_PERCENT
    
    # 4. スコアによる重み付け (線形補間)
    # スコアが SIGNAL_THRESHOLD_NORMAL (0.86) 以下なら最小ロット。
    # DYNAMIC_LOT_SCORE_MAX (0.96) で最大ロット。
    
    score_min = SIGNAL_THRESHOLD_NORMAL # 0.86点
    score_max = DYNAMIC_LOT_SCORE_MAX   # 0.96点
    
    # スコアが最小範囲以下の場合は、最小ロット (min_lot_from_equity) を使用
    if score <= score_min:
        dynamic_lot_usdt = min_lot_from_equity
    
    # スコアが最大範囲以上の場合は、最大ロット (max_lot_from_equity) を使用
    elif score >= score_max:
        dynamic_lot_usdt = max_lot_from_equity
        
    # スコアが最小範囲と最大範囲の間にある場合は、線形補間
    else:
        # スコアの比率: (score - score_min) / (score_max - score_min)
        ratio = (score - score_min) / (score_max - score_min)
        
        # ロットサイズの線形補間: V_min + (V_max - V_min) * ratio
        dynamic_lot_usdt = min_lot_from_equity + (max_lot_from_equity - min_lot_from_equity) * ratio

    # 5. 最終ロットサイズが、ベースロットサイズ (BASE_TRADE_SIZE_USDT) より小さい場合は、ベースロットを担保する
    final_lot_size = max(dynamic_lot_usdt, min_usdt_lot)
    
    # 6. 利用可能なUSDT残高を超えないようにクリッピング
    free_usdt = account_status.get('total_usdt_balance', 0.0)
    
    if final_lot_size > free_usdt:
        logging.warning(f"⚠️ 計算されたロット {format_usdt(final_lot_size)} USDT は残高 {format_usdt(free_usdt)} USDT を超えています。残高に合わせて調整します。")
        final_lot_size = free_usdt
    
    # 最小取引額の保証 (BASE_TRADE_SIZE_USDT)
    return max(final_lot_size, min_usdt_lot)

async def adjust_order_amount(symbol: str, lot_size_usdt: float, price: float) -> Tuple[float, float]:
    """取引所のルールに従って、USDT建てのロットサイズを基軸通貨数量と最終USDT額に調整する"""
    global EXCHANGE_CLIENT
    
    # 1. 注文数量の計算 (基軸通貨建て)
    if price <= 0:
        logging.error("❌ 価格がゼロまたはマイナスです。")
        return 0.0, 0.0
        
    base_amount_unrounded = lot_size_usdt / price
    
    # 2. 取引所の丸め精度と最小数量を取得
    market = EXCHANGE_CLIENT.markets.get(symbol)
    
    if not market or not market.get('limits') or not market['limits'].get('amount'):
        logging.error(f"❌ {symbol} の市場情報を取得できませんでした。取引をスキップします。")
        return 0.0, 0.0

    # 数量の丸め精度 (小数点以下の桁数)
    amount_precision = market['precision']['amount'] if market and market['precision'] else 8
    # 最小数量
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
            params={
                # TP注文を示すカスタムパラメータがあれば追加
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
        market = EXCHANGE_CLIENT.markets.get(symbol)
        
        if market and 'spot' in market['info'].get('permissions', []):
             # ccxtでstop_lossがサポートされていない場合、create_orderとparamsで対応
             # トリガー価格 = stop_loss (またはそれよりわずかに高い価格)
             # リミット価格 = ストップロス価格よりわずかに低い価格 (スリッページ対策)
             sl_limit_price = stop_loss * 0.999 # StopLoss価格の-0.01%をLimit価格とする
             
             # 価格の精度調整 (SL Limit Price)
             price_precision = market['precision']['price'] if market and market['precision'] else 8
             sl_limit_price = round(sl_limit_price, price_precision)

             sl_order = await EXCHANGE_CLIENT.create_order(
                 symbol=symbol,
                 type='stop_limit', # または 'stop_loss_limit'
                 side='sell',
                 amount=filled_amount,
                 price=sl_limit_price, # Limit Price
                 params={
                     'stopPrice': stop_loss, # Trigger Price
                     'clientOrderId': f'SL-{uuid.uuid4()}',
                 }
             )
             sl_order_id = sl_order['id']
             logging.info(f"✅ SL注文成功: ID={sl_order_id}, Trigger={format_price_precision(stop_loss)}, Limit={format_price_precision(sl_limit_price)}")
        else:
             # 現物でストップリミットがサポートされていない場合のフォールバック（ここではTPが既に成功しているため、エラーとする）
             raise ccxt.ExchangeError("Exchange does not support unified 'stop_limit' for spot.")
        
    except Exception as e:
        # SL設定失敗は致命的なので、先に設定したTP注文をキャンセルする
        logging.critical(f"🚨 SL注文失敗 ({symbol}): {e}")
        try:
            await EXCHANGE_CLIENT.cancel_order(tp_order_id, symbol)
            logging.warning(f"⚠️ SL設定失敗により、TP注文 (ID: {tp_order_id}) をキャンセルしました。")
        except Exception as cancel_e:
            logging.critical(f"❌ TP注文のキャンセルにも失敗 ({symbol}): {cancel_e}")
            
        return {'status': 'error', 'error_message': f'SL注文失敗: {e}'}

    return {
        'status': 'ok',
        'sl_order_id': sl_order_id,
        'tp_order_id': tp_order_id
    }

async def cancel_single_order(order_id: str, symbol: str) -> bool:
    """単一の注文をキャンセルする"""
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        return False
        
    try:
        await EXCHANGE_CLIENT.cancel_order(order_id, symbol)
        logging.info(f"✅ 注文キャンセル成功: {symbol}, ID={order_id}")
        return True
    except ccxt.OrderNotFound:
        logging.warning(f"⚠️ 注文キャンセル失敗: {symbol}, ID={order_id} (注文が見つかりませんでした)")
        return True # 見つからない=既にキャンセル/約定済みとしてOKとする
    except Exception as e:
        logging.error(f"❌ 注文キャンセル中にエラーが発生: {symbol}, ID={order_id}, Error: {e}")
        return False

async def close_position_immediately(symbol: str, amount: float) -> Dict:
    """不完全なポジションを成行売りで即時クローズする"""
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY or amount <= 0:
        return {'status': 'skipped', 'error_message': 'スキップ: クライアント未準備または数量がゼロです。'}
        
    logging.warning(f"🚨 不完全ポジションの強制クローズを試みます: {symbol} (数量: {amount:.4f})")
    
    try:
        # 数量の丸め（成行注文でも精度は重要）
        base_amount_rounded, _ = await adjust_order_amount(symbol, amount * 1.01 * EXCHANGE_CLIENT.markets[symbol]['info']['price'] , EXCHANGE_CLIENT.markets[symbol]['info']['price']) # 概算でロットサイズ計算
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

    Returns: 取引結果辞書
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
        # type='limit' と timeInForce: 'IOC' の組み合わせにより、指値注文（約定価格の確実性）かつ即時約定（残らない）
        order = await EXCHANGE_CLIENT.create_order(
            symbol=symbol,
            type='limit', 
            side='buy',
            amount=base_amount_to_buy,
            price=entry_price,
            params={
                'timeInForce': 'IOC' # Immediate Or Cancel
            }
        )
        
        order_id = order['id']
        filled_amount = order.get('filled', 0.0)
        
        if filled_amount > 0.0:
            # 4. IOC注文が約定した場合
            avg_fill_price = order.get('average', entry_price) # 平均約定価格
            filled_usdt = avg_fill_price * filled_amount
            
            # SL/TP注文の設定
            sl_tp_result = await place_sl_tp_orders(
                symbol=symbol,
                filled_amount=filled_amount,
                stop_loss=signal['stop_loss'],
                take_profit=signal['take_profit']
            )
            
            if sl_tp_result['status'] == 'ok':
                # SL/TP設定成功 -> ポジション管理リストに追加
                position_id = str(uuid.uuid4())
                OPEN_POSITIONS.append({
                    'id': position_id,
                    'symbol': symbol,
                    'filled_amount': filled_amount,
                    'filled_usdt': filled_usdt,
                    'entry_price': avg_fill_price,
                    'stop_loss': signal['stop_loss'],
                    'take_profit': signal['take_profit'],
                    'sl_order_id': sl_tp_result['sl_order_id'],
                    'tp_order_id': sl_tp_result['tp_order_id'],
                    'timestamp': time.time(),
                })
                logging.info(f"✅ ポジションオープン成功: {symbol} @ {format_price_precision(avg_fill_price)} (ID: {position_id})")
                return {
                    'status': 'ok',
                    'filled_amount': filled_amount,
                    'filled_usdt': filled_usdt,
                    'entry_price': avg_fill_price,
                    'sl_order_id': sl_tp_result['sl_order_id'],
                    'tp_order_id': sl_tp_result['tp_order_id'],
                    'close_status': 'skipped' # 正常終了時はクローズ処理はスキップ
                }
            else:
                # 5. IOC約定後のSL/TP設定失敗 -> ポジションを即時クローズ (成行売り)
                logging.critical(f"🚨 IOC約定 ({filled_amount:.4f}) 後にSL/TP設定に失敗しました。ポジションを即時クローズします。")
                close_result = await close_position_immediately(symbol, filled_amount)
                
                # 失敗の詳細を返す
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
        
        # 1. SL/TP注文のステータスチェック
        sl_status = await check_order_status(sl_order_id, symbol)
        tp_status = await check_order_status(tp_order_id, symbol)
        
        sl_open = sl_status is not None and sl_status.get('status') not in ['closed', 'canceled', 'expired']
        tp_open = tp_status is not None and tp_status.get('status') not in ['closed', 'canceled', 'expired']

        if not sl_open and not tp_open:
            # 1-a. 両方の決済注文が消滅（約定 or ユーザー手動キャンセル）
            is_closed = True
            exit_type = "取引所決済完了"
            logging.info(f"🔴 決済検出: {position['symbol']} - SL/TP注文が取引所から消滅。決済完了と見なします。")
            
        # 💡 V19.0.53 修正: 決済注文の不完全検出と再設定
        elif not sl_open or not tp_open: 
            # 2. 片方の決済注文が消滅または未設定 (再設定が必要なケース)
            
            # SL/TPのいずれかが約定したために片方が自動でキャンセルされた可能性を考慮
            
            # SL注文が「約定済み (closed/filled)」で、TPがオープンでない場合 -> SL約定と見なす
            if sl_status and sl_status.get('status') == 'closed' and sl_status.get('filled') > 0 and not tp_open:
                 is_closed = True
                 exit_type = "SL約定"
                 logging.warning(f"🔴 決済検出: {position['symbol']} - SL注文が約定しました。")
            
            # TP注文が「約定済み (closed/filled)」で、SLがオープンでない場合 -> TP約定と見なす
            elif tp_status and tp_status.get('status') == 'closed' and tp_status.get('filled') > 0 and not sl_open:
                 is_closed = True
                 exit_type = "TP約定"
                 logging.warning(f"🟢 決済検出: {position['symbol']} - TP注文が約定しました。")
            
            # それ以外の場合（片方が手動キャンセル、API接続断などで情報が欠けた場合）
            else:
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
    logging.debug(f"ℹ️ オープン注文監視ループ終了。残りのポジション: {len(OPEN_POSITIONS)}")


# ====================================================================================
# TELEGRAM NOTIFICATION
# ====================================================================================

async def send_telegram_notification(message: str):
    """Telegramにメッセージを送信する"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.warning("⚠️ TelegramのトークンまたはチャットIDが設定されていません。通知をスキップします。")
        return
        
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        'chat_id': TELEGRAM_CHAT_ID,
        'text': message,
        'parse_mode': 'HTML', # HTMLタグを有効にする
        'disable_web_page_preview': True
    }

    try:
        # リクエストは非同期で実行
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(None, lambda: requests.post(url, data=payload, timeout=10))
        response.raise_for_status() 
        logging.info("✅ Telegram通知を送信しました。")
    except requests.exceptions.HTTPError as e:
        logging.error(f"❌ Telegram通知の送信に失敗 (HTTPエラー): {response.status_code} - {response.text}")
    except Exception as e:
        logging.error(f"❌ Telegram通知の送信に失敗 (一般エラー): {e}")

# ====================================================================================
# MAIN BOT LOGIC
# ====================================================================================

async def analyze_symbol(symbol: str, market_ticker: Dict) -> List[Dict]:
    """指定されたシンボルと全てのタイムフレームで分析を実行する"""
    signals = []
    tasks = []
    
    # 全てのタイムフレームでOHLCV取得と分析を並行して実行
    for tf in TARGET_TIMEFRAMES:
        tasks.append(fetch_ohlcv_and_analyze(symbol, tf, market_ticker))
        
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    for result in results:
        if isinstance(result, Exception):
            # 例外が発生した場合
            error_message = str(result)
            # 特定のエラーを除外 (データ不足など)
            if 'Data insufficient' not in error_message:
                logging.error(f"❌ {symbol} - 分析タスクエラー: {error_message}")
        elif result:
            # シグナルが生成された場合
            signals.append(result)
            
    return signals

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
    
    # 5. 全ての監視銘柄の分析タスクを作成
    analysis_tasks = []
    
    # 1時間ごとのログをリセット (レポート送信後にリセットされる)
    if time.time() - LAST_HOURLY_NOTIFICATION_TIME >= HOURLY_SCORE_REPORT_INTERVAL:
         HOURLY_SIGNAL_LOG = []
         HOURLY_ATTEMPT_LOG = {}
    
    # クールダウンチェックと分析タスクの準備
    for symbol in CURRENT_MONITOR_SYMBOLS:
        
        # ティッカー情報がない銘柄はスキップ
        if symbol not in market_tickers:
             HOURLY_ATTEMPT_LOG[symbol] = "Ticker Missing"
             continue
             
        # クールダウンチェック: 前回のシグナル通知から2時間経過しているか
        last_signal_time = LAST_SIGNAL_TIME.get(symbol, 0.0)
        if (time.time() - last_signal_time) < TRADE_SIGNAL_COOLDOWN:
            cooldown_remaining = TRADE_SIGNAL_COOLDOWN - (time.time() - last_signal_time)
            HOURLY_ATTEMPT_LOG[symbol] = f"Cooldown ({cooldown_remaining/60:.0f}m)"
            continue
            
        # 分析タスクを追加
        analysis_tasks.append(analyze_symbol(symbol, market_tickers[symbol]))

    # 6. 並列で分析を実行
    all_results = await asyncio.gather(*analysis_tasks, return_exceptions=True)
    
    # 7. 結果を集約し、最高スコアのシグナルを特定
    all_signals: List[Dict] = []
    for results_list in all_results:
        if isinstance(results_list, list):
            all_signals.extend([s for s in results_list if s])

    # スコアでソートし、最高スコアを決定
    all_signals_sorted = sorted(all_signals, key=lambda x: x['score'], reverse=True)
    best_signal = all_signals_sorted[0] if all_signals_sorted else None
    
    # 8. 初回起動完了通知
    if not IS_FIRST_MAIN_LOOP_COMPLETED:
        notification_message = format_startup_message(
            account_status, 
            GLOBAL_MACRO_CONTEXT, 
            len(CURRENT_MONITOR_SYMBOLS), 
            current_threshold,
            BOT_VERSION
        )
        await send_telegram_notification(notification_message)
        IS_FIRST_MAIN_LOOP_COMPLETED = True
        
    # 9. 1時間ごとのレポート作成と通知
    HOURLY_SIGNAL_LOG.extend(all_signals) # ログに追加
    if time.time() - LAST_HOURLY_NOTIFICATION_TIME >= HOURLY_SCORE_REPORT_INTERVAL:
        logging.info("⏳ 1時間ごとのレポートを作成・送信します。")
        report_message = format_hourly_report(
            HOURLY_SIGNAL_LOG, 
            HOURLY_ATTEMPT_LOG, 
            LAST_HOURLY_NOTIFICATION_TIME, 
            current_threshold,
            BOT_VERSION
        )
        await send_telegram_notification(report_message)
        LAST_HOURLY_NOTIFICATION_TIME = time.time() # 通知時刻を更新
        HOURLY_SIGNAL_LOG = [] # ログをリセット
        HOURLY_ATTEMPT_LOG = {} # ログをリセット


    # 10. 取引の実行
    trade_result = None
    
    if best_signal:
        logging.info(f"🏆 最高スコアのシグナル: {best_signal['symbol']} ({best_signal['timeframe']}) - Score: {best_signal['score']*100:.2f}")

        if not TEST_MODE and best_signal['score'] >= current_threshold:
            # 取引実行
            
            # 残高チェック
            if account_status.get('total_usdt_balance', 0.0) >= MIN_USDT_BALANCE_FOR_TRADE:
                trade_result = await execute_trade(best_signal, account_status)
                
                if trade_result['status'] == 'ok':
                    # 取引成功した場合、クールダウン時間を更新
                    LAST_SIGNAL_TIME[best_signal['symbol']] = time.time()
                
                else:
                    # 取引失敗の場合、ログに記録するが、クールダウンは適用しない
                    pass 

            else:
                 error_message = f"残高不足 (現在: {format_usdt(account_status['total_usdt_balance'])} USDT)。新規取引に必要な額: {MIN_USDT_BALANCE_FOR_TRADE:.2f} USDT。"
                 trade_result = {'status': 'error', 'error_message': error_message, 'close_status': 'skipped'}
                 logging.warning(f"⚠️ {best_signal['symbol']} 取引スキップ: {error_message}")
        
        else:
            logging.info(f"ℹ️ {best_signal['symbol']} は閾値 {current_threshold*100:.2f} を満たしていません。取引をスキップします。")
            # 閾値を満たさないシグナルもログには記録する
            log_signal(best_signal, "取引シグナル (閾値未満/テストモード)")
        
        # 11. Telegram通知
        if trade_result and trade_result.get('status') == 'ok':
            # 取引成功
            # ★ v19.0.47 修正点: BOT_VERSION を明示的に渡す
            notification_message = format_telegram_message(best_signal, "取引シグナル", current_threshold, trade_result)
            await send_telegram_notification(notification_message)
            log_signal({**best_signal, **trade_result}, "取引シグナル (成功)")
            
        elif trade_result and trade_result.get('status') == 'error':
            # 取引失敗
            notification_message = format_telegram_message(best_signal, "取引シグナル", current_threshold, trade_result)
            await send_telegram_notification(notification_message)
            log_signal({**best_signal, **trade_result}, "取引シグナル (失敗)")

    logging.info("--- 💡 BOT LOOP END ---")

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
    
    # メインBOTループと注文監視ループをバックグラウンドで開始
    asyncio.create_task(main_bot_loop_scheduler())
    asyncio.create_task(open_order_management_scheduler())
    logging.info("✅ BOTメインタスクと注文監視タスクを開始しました。")

async def main_bot_loop_scheduler():
    """メインBOTループを定期実行するスケジューラ"""
    # 初回は即座に実行
    await main_bot_loop()
    
    while True:
        try:
            # メインループの実行
            await main_bot_loop() 
        except Exception as e:
            logging.critical(f"🚨 メインBOTループ中に致命的なエラーが発生: {e}", exc_info=True)
            
            # 致命的エラー発生をTelegramに通知
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
        "open_positions": OPEN_POSITIONS,
        "last_analysis_signals": LAST_ANALYSIS_SIGNALS # 直近の分析結果
    }
    
    return JSONResponse(content=status_data)

# if __name__ == "__main__":
#     # uvicorn.run("main_render:app", host="0.0.0.0", port=8000, log_level="info")
#     # RenderやDocker環境ではこの実行は不要
#     pass
