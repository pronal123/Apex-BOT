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
        f"  - **エントリー (Entry)**: <code>{format_price_precision(best_signal['entry_price'])}</code>\n"
        f"  - **SL/TP**: <code>{format_price_precision(best_signal['stop_loss'])}</code> / <code>{format_price_precision(best_signal['take_profit'])}</code>\n"
    )

    # ワーストシグナル (最低スコア、ただしベーススコア以上)
    worst_signals = [s for s in signals_sorted if s['score'] >= BASE_SCORE]
    if len(worst_signals) > 1:
        worst_signal = worst_signals[-1]
        message += (
            f"\n"
            f"🔴 **ワーストスコア銘柄 (Low)**\n"
            f"  - **銘柄**: <b>{worst_signal['symbol']}</b> ({worst_signal['timeframe']})\n"
            f"  - **スコア**: <code>{worst_signal['score'] * 100:.2f} / 100</code>\n"
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
        logging.warning(f"📉 {context}: {log_data['symbol']} (PnL: {log_data['pnl_percent']:+.2f}%)")
    elif context == "ポジション決済" and log_data['pnl_percent'] is not None and log_data['pnl_percent'] > 0:
        # プラス決済はINFO
        logging.info(f"📈 {context}: {log_data['symbol']} (PnL: {log_data['pnl_percent']:+.2f}%)")
    elif context == "取引シグナル" and log_data['trade_result_status'] == 'ok':
         logging.info(f"🔥 {log_data['symbol']} が閾値 {SIGNAL_THRESHOLD_NORMAL*100:.2f} を超えました (Score: {log_data['score']*100:.2f})。取引を実行します。")
    elif context == "取引シグナル":
         logging.debug(f"ℹ️ {log_data['symbol']} シグナルを記録 (Score: {log_data['score']*100:.2f})")
    else:
        logging.info(f"ℹ️ {context}: {log_data['symbol']} (Status: {log_data['trade_result_status']})")

    # JSON形式でファイルに書き出す処理は、ここでは省略（通常は別のサービス/ファイルで行う）
    pass 

# ====================================================================================
# EXCHANGE API WRAPPERS
# ====================================================================================

async def initialize_client():
    """CCXTクライアントを初期化する"""
    global EXCHANGE_CLIENT, IS_CLIENT_READY, CCXT_CLIENT_NAME
    
    if IS_CLIENT_READY and EXCHANGE_CLIENT:
        logging.info("ℹ️ CCXTクライアントは既に初期化されています。")
        return

    # クライアント名の小文字化
    client_name = CCXT_CLIENT_NAME.lower()

    if client_name not in ccxt.exchanges:
        logging.critical(f"🚨 {client_name.upper()} はサポートされていません。サポートされている取引所: {', '.join(ccxt.exchanges)}")
        sys.exit(1)

    try:
        # ccxt_asyncから非同期クライアントを取得
        exchange_class = getattr(ccxt_async, client_name)
        
        config = {
            'apiKey': API_KEY,
            'secret': SECRET_KEY,
            'enableRateLimit': True,
            'options': {
                # 現物取引モードを明示 (CCXTではデフォルトでSpotの場合が多いが、念のため)
                'defaultType': 'spot', 
            }
        }
        
        EXCHANGE_CLIENT = exchange_class(config)
        
        # ロードマーケットは必須
        await EXCHANGE_CLIENT.load_markets()
        
        # CCXTクライアントの準備完了
        IS_CLIENT_READY = True
        logging.info(f"✅ {client_name.upper()} クライアント初期化成功。取引モード: 現物 (Spot)。")

    except Exception as e:
        logging.critical(f"🚨 {client_name.upper()} クライアント初期化失敗: {e}", exc_info=True)
        sys.exit(1)


async def fetch_ohlcv(symbol: str, timeframe: str, limit: int = 500) -> Optional[pd.DataFrame]:
    """OHLCVデータを取得し、DataFrameとして返す"""
    global EXCHANGE_CLIENT
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.warning("⚠️ CCXTクライアントが初期化されていません。OHLCV取得をスキップします。")
        return None
    
    try:
        # 強制的にリロードしないように `params={'dont_reload': True}` を使う場合もあるが、ここではシンプルに。
        ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(symbol, timeframe, limit=limit)
        
        if not ohlcv or len(ohlcv) < limit:
            # 必要なデータ数が取得できなかった場合
            logging.warning(f"⚠️ {symbol} ({timeframe}): 必要なOHLCVデータ数 ({limit}) を取得できませんでした。取得数: {len(ohlcv) if ohlcv else 0}")
            return None
        
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df.set_index('timestamp', inplace=True)
        
        # 出来高がゼロのデータポイントが大量にある場合も除外すべきだが、ここではデータの存在のみチェック
        return df
        
    except Exception as e:
        # CCXTエラーまたはその他のエラー
        logging.error(f"❌ OHLCVデータ取得中にエラーが発生: {symbol} ({timeframe}), Error: {e}")
        return None


async def fetch_account_status() -> Dict:
    """口座残高を取得し、総資産額 (Equity) とUSDT残高を計算する"""
    global EXCHANGE_CLIENT, GLOBAL_TOTAL_EQUITY
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.warning("⚠️ CCXTクライアントが初期化されていません。口座ステータス取得をスキップします。")
        return {'total_usdt_balance': 0.0, 'total_equity': 0.0, 'open_positions': [], 'error': True}
    
    try:
        # fetch_balanceで全ての残高を取得
        balance = await EXCHANGE_CLIENT.fetch_balance()
        
        # USDT残高 (Free)
        total_usdt_balance = balance.get('free', {}).get('USDT', 0.0)
        
        # 総資産額 (Equity) の計算
        total_equity = total_usdt_balance
        
        # USDT以外の保有資産をUSDT建てで評価し、合算する
        # 'total'キーには、Free + Used が含まれる
        for currency, amount in balance.get('total', {}).items():
            if currency not in ['USDT', 'USD'] and amount is not None and amount > 0.000001:
                symbol = f"{currency}/USDT"
                try:
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
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY or SKIP_MARKET_UPDATE:
        logging.info("ℹ️ Yfinanceデータ取得をスキップしました (間隔内)。キャッシュされた為替データ Bonus=-0.0063 を使用します。")
        return DEFAULT_SYMBOLS.copy()

    try:
        # CCXTのfetch_tickersを使って全USDTペアのティッカーを取得
        tickers = await EXCHANGE_CLIENT.fetch_tickers()
        
        usdt_pairs = {}
        for symbol, ticker in tickers.items():
            # 出来高 (quoteVolume, USDT建て) があり、'USDT'ペアであること
            if '/USDT' in symbol and ticker and ticker.get('quoteVolume') is not None:
                usdt_pairs[symbol] = ticker['quoteVolume']

        # 出来高でソートし、TOP Nを取得
        sorted_pairs = sorted(usdt_pairs.items(), key=lambda x: x[1], reverse=True)
        top_symbols = [symbol for symbol, volume in sorted_pairs[:TOP_SYMBOL_LIMIT]]
        
        # デフォルトシンボルと重複しないものを結合
        combined_symbols = list(set(top_symbols + DEFAULT_SYMBOLS))
        
        logging.info(f"✅ 出来高TOPシンボル更新成功 (TOP {len(top_symbols)} + Default {len(DEFAULT_SYMBOLS)} = Total {len(combined_symbols)})")
        return combined_symbols

    except Exception as e:
        logging.error(f"❌ 出来高TOPシンボル取得中にエラーが発生: {e}")
        return DEFAULT_SYMBOLS.copy()


async def fetch_fgi_data() -> Dict:
    """FGI (Fear & Greed Index) と為替データを取得し、スコアリング用のプロキシ値を返す"""
    global GLOBAL_MACRO_CONTEXT, LAST_SUCCESS_TIME, SKIP_MARKET_UPDATE
    
    # リアルなAPIコールをスキップする設定の場合、キャッシュされた値を使用
    if SKIP_MARKET_UPDATE:
        logging.info("ℹ️ Yfinanceデータ取得をスキップしました (間隔内)。キャッシュされた為替データ Bonus=-0.0063 を使用します。")
        return GLOBAL_MACRO_CONTEXT
        
    fgi_proxy = 0.0
    fgi_raw_value = 'N/A'
    forex_bonus = 0.0
    
    # 1. FGI (Fear & Greed Index) の取得
    fgi_url = "https://api.alternative.me/fng/?limit=1"
    try:
        response = requests.get(fgi_url, timeout=5)
        response.raise_for_status()
        data = response.json()
        
        if data.get('data'):
            index_value = int(data['data'][0]['value'])
            # 恐怖 (0) から貪欲 (100) を -0.05 から +0.05 の範囲に正規化
            # (50を0、0を-0.05、100を+0.05とする)
            fgi_raw_value = index_value
            fgi_proxy = (index_value - 50) / 50 * FGI_PROXY_BONUS_MAX
            logging.info(f"✅ FGIプロキシ取得成功: Raw Value={index_value}, Proxy={fgi_proxy:.4f}")
        
    except Exception as e:
        logging.error(f"❌ FGIデータ取得中にエラーが発生: {e}")
        # 失敗した場合は、全て0.0を返す
        pass
        
    # 2. 為替データ (USDJPY) の取得 (代替データとして実装を続ける)
    # yfinanceモジュールの使用は避ける
    try:
        # USDJPYの過去データに基づく簡易的な市場センチメント評価をシミュレーション
        # (ここでは具体的な外部APIコールは避け、ランダムな値を使用する)
        
        # NOTE: 実際の稼働環境では、ここで為替レートまたはマクロ経済指標のAPIを叩く
        
        # シンプルなランダム変動をシミュレーション（-0.005から+0.005の範囲）
        forex_bonus = random.uniform(-0.005, 0.005) 
        # forex_bonus = -0.0063 # テスト用固定値

    except Exception as e:
        # 為替データ取得失敗は致命的ではない
        logging.warning(f"⚠️ 為替データ取得中にエラーが発生: {e}")
        forex_bonus = 0.0 # 失敗した場合は0.0とする

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
        df['BBL'] = bb_data['BBL_20_2.0_2.0']
        df['BBM'] = bb_data['BBM_20_2.0']
        df['BBU'] = bb_data['BBU_20_2.0_2.0']
    elif bb_data is not None and not bb_data.empty and 'BBL_20_2.0' in bb_data.columns:
        # 古いバージョン対応
        df['BBL'] = bb_data['BBL_20_2.0']
        df['BBM'] = bb_data['BBM_20_2.0']
        df['BBU'] = bb_data['BBU_20_2.0']
    else:
         df['BBL'] = np.nan
         df['BBM'] = np.nan
         df['BBU'] = np.nan
         
    # Average True Range (ATR) - ボラティリティ測定
    df['ATR'] = ta.atr(df['high'], df['low'], df['close'], length=14)
    
    # On Balance Volume (OBV)
    df['OBV'] = ta.obv(df['close'], df['volume'])
    
    # 必要なデータポイントのみを残す (dropnaではなく、必要な期間の後に開始)
    df = df.iloc[LONG_TERM_SMA_LENGTH:]
    
    return df

def score_signal(df: pd.DataFrame, timeframe: str, market_ticker: Dict, macro_context: Dict) -> Optional[Dict]:
    """DataFrameとティッカー情報に基づいて取引シグナルのスコアリングを行う (ロング目線)"""
    
    # 1. データの最終チェック
    if df.empty or len(df) < 5: 
        # logging.warning(f"⚠️ {market_ticker['symbol']} ({timeframe}): データ不足または指標計算失敗。")
        return None
        
    last_candle = df.iloc[-1]
    current_price = market_ticker['last']
    
    # 2. エントリー条件の定義 (ロングエントリー)
    
    # 2-1. 基本的なモメンタム: 現在のローソク足の終値が前のローソク足の終値よりも高い
    # 強いモメンタムが確認できる、または価格が長期SMAの上にある
    is_price_increasing = last_candle['close'] > df.iloc[-2]['close']
    is_above_long_term_sma = current_price > last_candle['SMA_200']
    
    # RSIが過熱圏でない (70未満)
    is_rsi_not_overbought = last_candle['RSI'] < 70 
    
    # 価格がBB下限に近い、またはBB中心線の上で推移
    is_near_bb_low = current_price < last_candle['BBL'] * 1.005 # BB下限から0.5%以内
    is_above_bbm = current_price > last_candle['BBM'] # BB中心線の上

    # 必須エントリー条件 (ここでは、価格が長期SMAを上回っていること、またはRSIが過熱圏でないことのみを必須とする)
    if not (is_above_long_term_sma or is_near_bb_low):
        # 厳密なエントリー条件を満たさない場合はシグナルなし
        # logging.debug(f"ℹ️ {market_ticker['symbol']} ({timeframe}): エントリー基本条件を満たしません。")
        return None
        
    # 3. リスク管理設定 (SL/TP)
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
    if last_candle['SMA_50'] > last_candle['SMA_200']:
        trend_alignment_bonus_value = TREND_ALIGNMENT_BONUS
        total_score += trend_alignment_bonus_value
    tech_data['trend_alignment_bonus_value'] = trend_alignment_bonus_value

    # D. 価格構造/ピボット支持ボーナス (6点)
    # 直近の安値/高値、またはSMA50/SMA20の近くに価格があることを確認 (ここではSMA50で簡易化)
    structural_pivot_bonus = 0.0
    # 価格がSMA50よりわずかに上
    if current_price > last_candle['SMA_50'] and current_price < last_candle['SMA_50'] * 1.002: 
        structural_pivot_bonus = STRUCTURAL_PIVOT_BONUS
        total_score += structural_pivot_bonus
    tech_data['structural_pivot_bonus'] = structural_pivot_bonus

    # E. MACDペナルティ (25点)
    macd_penalty_value = 0.0
    # MACDラインがシグナルラインを下回っている（弱気クロス）またはヒストグラムが負で減少している（弱気発散）
    is_macd_bearish_cross = last_candle['MACD'] < last_candle['MACDs']
    is_macd_histogram_decreasing = last_candle['MACDh'] < df.iloc[-2]['MACDh'] and last_candle['MACDh'] < 0
    
    if is_macd_bearish_cross or is_macd_histogram_decreasing:
        macd_penalty_value = MACD_CROSS_PENALTY
        total_score -= macd_penalty_value
    tech_data['macd_penalty_value'] = macd_penalty_value
    
    # F. RSIモメンタムボーナス (10点)
    # RSIがRSI_MOMENTUM_LOW (45)を下回っている場合、モメンタムが低下していると見なす
    rsi_momentum_bonus_value = 0.0
    rsi_value = last_candle['RSI']
    tech_data['rsi_value'] = rsi_value

    if rsi_value > RSI_MOMENTUM_LOW and rsi_value < 70:
        # RSIが45から70の間で高いほどボーナス
        bonus_factor = (rsi_value - RSI_MOMENTUM_LOW) / (70 - RSI_MOMENTUM_LOW)
        rsi_momentum_bonus_value = RSI_MOMENTUM_BONUS_MAX * bonus_factor
        total_score += rsi_momentum_bonus_value
    tech_data['rsi_momentum_bonus_value'] = rsi_momentum_bonus_value

    # G. 出来高/OBV確証ボーナス (5点)
    # OBVが上昇傾向にある (直近のN期間の終値で増加)
    obv_momentum_bonus_value = 0.0
    obv_change = last_candle['OBV'] - df.iloc[-10]['OBV'] # 直近10期間のOBV変化
    if obv_change > 0:
        obv_momentum_bonus_value = OBV_MOMENTUM_BONUS
        total_score += obv_momentum_bonus_value
    tech_data['obv_momentum_bonus_value'] = obv_momentum_bonus_value

    # H. 出来高スパイクボーナス (7点)
    # 直近の出来高が過去の平均を上回っている
    volume_increase_bonus_value = 0.0
    avg_volume = df['volume'].iloc[-30:-1].mean()
    current_volume = last_candle['volume']
    
    if current_volume > avg_volume * 1.5: # 過去30期間の平均の1.5倍以上
        volume_increase_bonus_value = VOLUME_INCREASE_BONUS
        total_score += volume_increase_bonus_value
    tech_data['volume_increase_bonus_value'] = volume_increase_bonus_value

    # I. 流動性ボーナス (7点)
    # 板の厚み (ティッカーの Bid/Ask スプレッドが狭い) に応じたボーナス
    liquidity_bonus_value = 0.0
    if market_ticker['bid'] and market_ticker['ask']:
        spread_percent = (market_ticker['ask'] - market_ticker['bid']) / market_ticker['ask']
        
        # スプレッドが0.1%未満であれば最大ボーナス
        if spread_percent < 0.001: 
            liquidity_bonus_value = LIQUIDITY_BONUS_MAX
        # スプレッドが0.5%未満であれば、部分的なボーナス
        elif spread_percent < 0.005:
             bonus_factor = 1.0 - (spread_percent - 0.001) / 0.004 # 0.001で1.0、0.005で0.0
             liquidity_bonus_value = LIQUIDITY_BONUS_MAX * bonus_factor
             
        total_score += liquidity_bonus_value
    tech_data['liquidity_bonus_value'] = liquidity_bonus_value

    # J. ボラティリティペナルティ (低ボラティリティ)
    # ATR/価格が低すぎる場合 (BB幅が狭すぎる場合) にペナルティ
    volatility_penalty_value = 0.0
    if not np.isnan(last_candle['BBU']) and not np.isnan(last_candle['BBL']):
        bb_width_ratio = (last_candle['BBU'] - last_candle['BBL']) / last_candle['BBM']
        if bb_width_ratio < VOLATILITY_BB_PENALTY_THRESHOLD:
            # BB幅が1%未満の場合、ペナルティ
            volatility_penalty_value = -(LONG_TERM_REVERSAL_PENALTY / 2.0) # 最大ペナルティの半分
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
    
    min_score = SIGNAL_THRESHOLD_NORMAL # 86点
    max_score = DYNAMIC_LOT_SCORE_MAX # 96点
    
    if score <= min_score:
        # 閾値以下の場合は最小ロット
        target_lot = min_lot_from_equity
    elif score >= max_score:
        # 最大スコア以上の場合は最大ロット
        target_lot = max_lot_from_equity
    else:
        # 線形補間
        ratio = (score - min_score) / (max_score - min_score)
        target_lot = min_lot_from_equity + (max_lot_from_equity - min_lot_from_equity) * ratio

    # 5. 最終ロットの決定
    # 計算されたロットサイズは、最小ロット (BASE_TRADE_SIZE_USDT) を下回ってはならない
    final_lot_size = max(target_lot, min_usdt_lot)
    
    # 実行可能なUSDT残高を超えないようにクリップ
    free_usdt = account_status.get('total_usdt_balance', 0.0)
    final_lot_size = min(final_lot_size, free_usdt)
    
    # 最小取引サイズ (10 USDT) も考慮に入れる
    min_trade_size = 10.0 # 取引所ごとの最小取引額 (ccxtで取得するのが理想だが、ここでは定数)
    if final_lot_size < min_trade_size:
        return 0.0 # 取引しない

    return final_lot_size


async def adjust_order_amount(symbol: str, usdt_amount: float, price: float) -> Tuple[float, float]:
    """USDT建てのロットサイズと価格から、取引所ルールに基づいて数量と最終的なUSDT金額を調整する"""
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT or not symbol or usdt_amount <= 0 or price <= 0:
        return 0.0, 0.0
        
    try:
        # 取引所のシンボル情報から、数量と価格の最小単位（精度）を取得
        market = EXCHANGE_CLIENT.markets.get(symbol)
        if not market:
            logging.error(f"❌ {symbol} の市場情報が見つかりません。")
            return 0.0, 0.0

        # USDT金額から概算のベース通貨数量を計算
        base_amount = usdt_amount / price
        
        # 数量の精度（小数点以下の桁数）で丸める
        # amount_to_precision(amount, market['precision']['amount']) を使用するのが定石
        precision = market['precision'].get('amount')
        
        if precision is not None:
            # ccxtのユーティリティ関数を使って丸め
            base_amount_rounded = EXCHANGE_CLIENT.amount_to_precision(base_amount, precision)
        else:
             # 精度情報がない場合のフォールバック（小数点第4位）
            base_amount_rounded = math.floor(base_amount * 10000) / 10000 
            
        # 最小取引数量のチェック (min_amount)
        min_amount = market.get('limits', {}).get('amount', {}).get('min', 0.0)
        
        if base_amount_rounded < min_amount:
            logging.warning(f"⚠️ {symbol}: 数量 {base_amount_rounded:.8f} は最小数量 {min_amount:.8f} を満たしません。")
            return 0.0, 0.0
            
    except Exception as e:
        logging.error(f"❌ 注文数量調整中にエラーが発生 ({symbol}): {e}")
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
             
             # トリガー価格 (stop_loss) とリミット価格を設定
             # リミット価格はトリガー価格よりわずかに低く設定（より確実に約定させるため）
             limit_price = stop_loss * 0.999 # 0.1%ディスカウント
             
             # MEXCの場合、`stop`タイプの注文に`triggerPrice`を設定
             # ccxtには unified `stopLoss` メソッドがあるため、それを使用することが推奨される
             
             # CCXT Unified Stop-Loss Call (取引所がサポートしている場合)
             # 現物取引で統一されたストップリミット注文をサポートしている取引所は限られるため、ここでは汎用的な `create_order` + `params` を使用
             
             # 例: MEXCのTrigger Order
             sl_order = await EXCHANGE_CLIENT.create_order(
                symbol=symbol,
                type='stop_limit', # または 'stop'
                side='sell',
                amount=filled_amount,
                price=limit_price, # 実際に約定する価格
                params={
                    'stopPrice': stop_loss, # トリガー価格
                    'clientOrderId': f'SL-{uuid.uuid4()}'
                }
             )
             sl_order_id = sl_order['id']
             logging.info(f"✅ SL注文成功: ID={sl_order_id}, Trigger={format_price_precision(stop_loss)}, Limit={format_price_precision(limit_price)}")
        
        else:
             # サポートされていない場合は警告 (ただし、TPは設定済み)
             logging.warning(f"⚠️ {symbol}: 取引所がunified Stop-Limit注文をサポートしていない、またはカスタム実装が必要です。SL注文はスキップされました。")
             sl_order_id = "SKIPPED-NO_SUPPORT"

    except Exception as e:
        # SL設定失敗は致命的だが、TPはキャンセルしない（手動でのSL設定を促す）
        logging.error(f"❌ SL注文失敗 ({symbol}): {e}", exc_info=True)
        return {'status': 'error', 'error_message': f'SL注文失敗: {e}'}

    return {'status': 'ok', 'sl_order_id': sl_order_id, 'tp_order_id': tp_order_id}


async def close_position_immediately(symbol: str, amount: float) -> Dict:
    """ SL/TP注文の設定に失敗した場合などに、不完全なポジションを成行売りで即時クローズする。 """
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY or amount <= 0:
        return {'status': 'skipped', 'error_message': 'スキップ: クライアント未準備または数量がゼロです。'}

    logging.warning(f"🚨 不完全ポジションの強制クローズを試みます: {symbol} (数量: {amount:.4f})")
    
    try:
        # 数量の丸め（成行注文でも精度は重要）
        # adjust_order_amountを使用し、現在の市場価格で数量を再調整することが必要だが、
        # ここでは概算でロットサイズ計算のために価格情報を使用（市場価格は手動で取得が必要だが、ここでは簡易化）
        
        # Note: 厳密には、ここで最新の市場価格を取得し、adjust_order_amountに渡すべき。
        # market = EXCHANGE_CLIENT.markets[symbol]
        # ticker = await EXCHANGE_CLIENT.fetch_ticker(symbol)
        # current_price = ticker['last']
        # base_amount_rounded, _ = await adjust_order_amount(symbol, amount * current_price, current_price)
        
        # 簡略化のため、ロットサイズ計算に市場価格を暫定的に使用
        market = EXCHANGE_CLIENT.markets[symbol]
        base_amount_rounded = EXCHANGE_CLIENT.amount_to_precision(amount, market['precision']['amount'])

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
    
    logging.info(f"⏳ 現物指値買い注文 (IOC) 実行中... Qty={base_amount_to_buy:.4f}, Price={format_price_precision(entry_price)}")
    
    # 💡【重要: NoneTypeエラー回避のための初期化】
    # この変数がtryブロックで設定されなかった場合に備えて初期化が必要です。
    filled_amount = 0.0
    filled_usdt = 0.0
    order_id = None
    
    try:
        # IOC (Immediate-Or-Cancel) 指値買い注文
        order = await EXCHANGE_CLIENT.create_order(
            symbol=symbol,
            type='limit', # limit order
            side='buy',
            amount=base_amount_to_buy,
            price=entry_price,
            params={'timeInForce': 'IOC'} # IOC注文
        )
        
        order_id = order['id']
        # 約定数量の確認 (IOC注文は即座に約定/キャンセルされる)
        filled_amount = order.get('filled', 0.0) 
        filled_usdt = order.get('cost', 0.0)
        
        # 部分約定または全約定した場合
        if filled_amount > 0.0:
            # SL/TP注文の設定
            sl_tp_result = await place_sl_tp_orders(
                symbol=symbol,
                filled_amount=filled_amount,
                stop_loss=signal['stop_loss'],
                take_profit=signal['take_profit']
            )
            
            if sl_tp_result['status'] == 'ok':
                # 取引成功。ポジションをグローバルリストに追加
                position_id = str(uuid.uuid4())
                OPEN_POSITIONS.append({
                    'id': position_id,
                    'symbol': symbol,
                    'entry_price': entry_price,
                    'filled_amount': filled_amount,
                    'filled_usdt': filled_usdt,
                    'stop_loss': signal['stop_loss'],
                    'take_profit': signal['take_profit'],
                    'sl_order_id': sl_tp_result['sl_order_id'],
                    'tp_order_id': sl_tp_result['tp_order_id'],
                    'entry_timestamp': time.time()
                })
                
                return {
                    'status': 'ok',
                    'filled_amount': filled_amount,
                    'filled_usdt': filled_usdt,
                    'entry_price': entry_price,
                    'stop_loss': signal['stop_loss'],
                    'take_profit': signal['take_profit'],
                    'sl_order_id': sl_tp_result['sl_order_id'],
                    'tp_order_id': sl_tp_result['tp_order_id'],
                    'position_id': position_id
                }
            else:
                # SL/TP注文の設定に失敗した場合、残ったポジションを即時クローズ
                logging.critical(f"🚨 SL/TP注文設定失敗: {sl_tp_result['error_message']}。ポジションを強制クローズします。")
                close_result = await close_position_immediately(symbol, filled_amount)
                
                return {
                    'status': 'error',
                    'error_message': f"SL/TP注文設定失敗: {sl_tp_result['error_message']}",
                    'close_status': close_result['status'],
                    'closed_amount': close_result.get('closed_amount', 0.0),
                    'close_error_message': close_result.get('error_message'),
                    'filled_amount': filled_amount,
                    'filled_usdt': filled_usdt,
                }

        else:
            # 約定数量がゼロの場合（IOC注文で即時約定しなかった）
            error_message = f"指値買い注文が約定しませんでした (約定数量: {filled_amount:.4f})。"
            logging.info(f"❌ 取引実行失敗 ({symbol}): {error_message}")
            # 注文はキャンセル済みと見なされるため、クローズは不要
            return {'status': 'error', 'error_message': error_message, 'close_status': 'skipped'}
            
    except ccxt.ArgumentsRequired as e:
        error_message = f"引数不足エラー: {e}"
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
        
    if order_id == "SKIPPED-NO_SUPPORT":
        # SL注文をスキップした場合は、常にオープン中と見なして強制監視
        return {'status': 'open'} 

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
        
        sl_open = sl_status and sl_status.get('status') == 'open'
        tp_open = tp_status and tp_status.get('status') == 'open'
        
        # 2. 決済ロジック
        if sl_status and sl_status.get('status') == 'closed':
            # SLが約定
            if tp_open:
                # TP注文をキャンセル
                try:
                    await EXCHANGE_CLIENT.cancel_order(tp_order_id, symbol)
                    logging.info(f"✅ SL約定に伴い、TP注文 (ID: {tp_order_id}) をキャンセルしました。")
                except Exception as e:
                    logging.warning(f"⚠️ TP注文のキャンセルに失敗 ({symbol}, ID: {tp_order_id}): {e}")
            is_closed = True
            exit_type = 'SL約定'
            
        elif tp_status and tp_status.get('status') == 'closed':
            # TPが約定
            if sl_open:
                # SL注文をキャンセル
                try:
                    # SL注文は stop_limit の場合、種類に応じてキャンセルが必要
                    await EXCHANGE_CLIENT.cancel_order(sl_order_id, symbol)
                    logging.info(f"✅ TP約定に伴い、SL注文 (ID: {sl_order_id}) をキャンセルしました。")
                except Exception as e:
                    logging.warning(f"⚠️ SL注文のキャンセルに失敗 ({symbol}, ID: {sl_order_id}): {e}")
            is_closed = True
            exit_type = 'TP約定'
        
        # 💡【V19.0.53-p1で追加】 SL/TP注文の再設定ロジック
        elif (sl_order_id != "SKIPPED-NO_SUPPORT" and not sl_open and not tp_open):
             # SLもTPもclosed/canceled/rejectedだが、約定による決済でない場合（例: 取引所側で勝手にキャンセルされた、ユーザー手動決済など）
             
             # 市場価格でポジションが残っているか再確認が必要だが、ここでは取引所側の注文IDが消えたことを決済トリガーとする
             # (より厳密には、fetch_balanceでポジションが残っているか確認すべき)
             logging.info(f"ℹ️ {symbol}: SL/TP注文が両方ともクローズされています。外部決済と見なします。")
             is_closed = True
             exit_type = '外部決済/手動決済'

        elif (sl_order_id != "SKIPPED-NO_SUPPORT" and (sl_open or tp_open)):
            # 注文が片方または両方欠けている場合に再設定を試みる (SL/TP注文の失敗や期限切れに対応)
            should_recreate_sl = not sl_open and sl_order_id != "SKIPPED-NO_SUPPORT" # SLが存在せず、スキップされたわけではない
            should_recreate_tp = not tp_open
            
            if should_recreate_sl or should_recreate_tp:
                logging.warning(f"⚠️ {symbol}: SL({sl_open}) または TP({tp_open}) 注文が欠落しています。再設定を試みます。")
                
                # 残っている注文をキャンセル
                if sl_open:
                    try:
                        await EXCHANGE_CLIENT.cancel_order(sl_order_id, symbol)
                    except: pass
                if tp_open:
                    try:
                        await EXCHANGE_CLIENT.cancel_order(tp_order_id, symbol)
                    except: pass
                
                # SL/TP注文を再設定
                re_place_result = await place_sl_tp_orders(
                    symbol=symbol,
                    filled_amount=position['filled_amount'],
                    stop_loss=position['stop_loss'],
                    take_profit=position['take_profit']
                )
                
                if re_place_result['status'] == 'ok':
                    # IDを更新
                    position['sl_order_id'] = re_place_result['sl_order_id']
                    position['tp_order_id'] = re_place_result['tp_order_id']
                    logging.info(f"✅ {symbol}: SL/TP注文の再設定に成功しました。")
                else:
                    # 再設定に失敗した場合は警告ログ
                    logging.error(f"❌ {symbol}: SL/TP注文の再設定に失敗しました: {re_place_result['error_message']}")

        else:
            # 注文は引き続きオープン中
            logging.debug(f"ℹ️ {symbol} は引き続きオープン中 (SL: {sl_open}, TP: {tp_open})")
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


async def analyze_symbol_and_timeframes(symbol: str, market_ticker: Dict, macro_context: Dict) -> List[Dict]:
    """特定の銘柄に対し、全てのタイムフレームで分析を実行する"""
    signals = []
    
    # 複数のOHLCVデータを並列で取得・分析
    analysis_tasks = []
    for tf in TARGET_TIMEFRAMES:
        task = analyze_single_timeframe(symbol, tf, market_ticker, macro_context)
        analysis_tasks.append(task)
        
    results = await asyncio.gather(*analysis_tasks)
    
    for result in results:
        if result:
            signals.append(result)
            
    return signals


async def analyze_single_timeframe(symbol: str, timeframe: str, market_ticker: Dict, macro_context: Dict) -> Optional[Dict]:
    """単一のタイムフレームでOHLCVを取得・指標計算・スコアリングを行う"""
    try:
        # 1. OHLCVデータを取得
        limit = REQUIRED_OHLCV_LIMITS.get(timeframe, 500)
        df = await fetch_ohlcv(symbol, timeframe, limit=limit)
        
        if df is None:
            # logging.debug(f"ℹ️ {symbol} ({timeframe}): OHLCVデータ取得失敗または不足。")
            return None
        
        # 2. テクニカル指標を計算
        # 計算中に元のDFを変更しないようコピー
        df_copy = df.copy() 
        df_ta = calculate_indicators(df_copy) 
        
        if df_ta is None or df_ta.empty:
            # logging.debug(f"ℹ️ {symbol} ({timeframe}): テクニカル指標計算失敗。")
            return None
            
        # 3. スコアリング
        signal = score_signal(df_ta, timeframe, market_ticker, macro_context)
        
        return signal

    except Exception as e:
        # このエラーは analyze_symbol でキャッチされる
        raise Exception(f"OHLCV Fetch/Indicator Calc Error for {symbol} ({timeframe}): {e}")

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

    # 5. 全ての監視銘柄の分析タスクを作成
    analysis_tasks = []
    
    # ログをクリア
    HOURLY_SIGNAL_LOG = []
    HOURLY_ATTEMPT_LOG = {}
    
    # 並列実行する銘柄をフィルタリング
    symbols_to_analyze = [s for s in CURRENT_MONITOR_SYMBOLS if s in market_tickers]
    
    for symbol in symbols_to_analyze:
        # 過去2時間以内に取引通知を出していない銘柄のみを本格的に分析する
        if symbol in LAST_SIGNAL_TIME and (time.time() - LAST_SIGNAL_TIME[symbol]) < TRADE_SIGNAL_COOLDOWN:
            HOURLY_ATTEMPT_LOG[symbol] = 'Cooldown'
            continue
            
        ticker = market_tickers[symbol]
        # 出来高がゼロ、または価格情報がない銘柄はスキップ
        if ticker.get('quoteVolume', 0.0) == 0.0 or ticker.get('last') is None:
            HOURLY_ATTEMPT_LOG[symbol] = 'Low Volume/Price'
            continue
            
        analysis_tasks.append(analyze_symbol_and_timeframes(symbol, ticker, GLOBAL_MACRO_CONTEXT))

    logging.info(f"⏳ {len(analysis_tasks)} 個のOHLCVデータ取得と分析タスクを実行中.")
    
    # 6. 全ての分析を並列実行
    all_signals: List[Dict] = []
    try:
        results = await asyncio.gather(*analysis_tasks)
        for signals_list in results:
            all_signals.extend(signals_list)
    except Exception as e:
        logging.error(f"❌ 並列分析中にエラーが発生: {e}")

    # 7. 全てのシグナルをスコアでソート
    all_signals.sort(key=lambda x: x['score'], reverse=True)
    
    # ベーススコア以上のシグナルをフィルタリング
    valid_signals = [s for s in all_signals if s['score'] >= BASE_SCORE] 
    LAST_ANALYSIS_SIGNALS = valid_signals # グローバル変数に保存
    HOURLY_SIGNAL_LOG.extend(valid_signals) # 1時間ログに追加

    # 8. 最適な取引シグナルを選択
    best_signal = None
    for signal in valid_signals:
        # 最適なシグナル = 閾値を超えており、クールダウン期間中でないもの
        if signal['score'] >= current_threshold and (signal['symbol'] not in LAST_SIGNAL_TIME or (time.time() - LAST_SIGNAL_TIME[signal['symbol']]) >= TRADE_SIGNAL_COOLDOWN):
            best_signal = signal
            break

    # 9. 初回起動完了通知
    if not IS_FIRST_MAIN_LOOP_COMPLETED:
        startup_message = format_startup_message(account_status, GLOBAL_MACRO_CONTEXT, len(CURRENT_MONITOR_SYMBOLS), current_threshold, BOT_VERSION)
        await send_telegram_notification(startup_message)
        IS_FIRST_MAIN_LOOP_COMPLETED = True

    # 10. 取引実行
    trade_result = None
    if best_signal:
        logging.info(f"🔥 {best_signal['symbol']}/{best_signal['timeframe']} が閾値 {current_threshold*100:.2f} を超えました (Score: {best_signal['score']*100:.2f})。取引を実行します。")
        
        # テストモードまたは残高チェック
        if TEST_MODE:
             trade_result = {'status': 'error', 'error_message': 'TEST_MODE is active.', 'close_status': 'skipped'}
        elif account_status.get('total_usdt_balance', 0.0) >= MIN_USDT_BALANCE_FOR_TRADE:
            # 取引実行
            trade_result = await execute_trade(best_signal, account_status)
            
            if trade_result['status'] == 'ok':
                LAST_SIGNAL_TIME[best_signal['symbol']] = time.time() # 取引成功時にクールダウンタイマーをリセット
            log_signal(best_signal, "取引シグナル")
            
        else:
            error_message = f"残高不足 (現在: {format_usdt(account_status['total_usdt_balance'])} USDT)。新規取引に必要な額: {MIN_USDT_BALANCE_FOR_TRADE:.2f} USDT。"
            trade_result = {'status': 'error', 'error_message': error_message, 'close_status': 'skipped'}
            logging.warning(f"⚠️ {best_signal['symbol']} 取引スキップ: {error_message}")
    else:
        # 閾値を満たさないシグナルもログには記録する
        # 最もスコアの高いシグナルが閾値未満の場合のみログに記録
        if valid_signals and valid_signals[0]['score'] < current_threshold:
            log_signal(valid_signals[0], "取引シグナル (閾値未満/テストモード)")
        
    # 11. Telegram通知
    if trade_result and trade_result.get('status') == 'ok':
        # 取引成功
        # ★ v19.0.47 修正点: BOT_VERSION を明示的に渡す
        notification_message = format_telegram_message(best_signal, "取引シグナル", current_threshold, trade_result)
        await send_telegram_notification(notification_message)
    elif trade_result and trade_result.get('status') == 'error':
        # 取引失敗
        notification_message = format_telegram_message(best_signal, "取引シグナル", current_threshold, trade_result)
        await send_telegram_notification(notification_message)
    
    # 12. 1時間ごとのレポート通知
    if time.time() - LAST_HOURLY_NOTIFICATION_TIME > HOURLY_SCORE_REPORT_INTERVAL:
        hourly_report = format_hourly_report(HOURLY_SIGNAL_LOG, HOURLY_ATTEMPT_LOG, LAST_HOURLY_NOTIFICATION_TIME, current_threshold, BOT_VERSION)
        await send_telegram_notification(hourly_report)
        LAST_HOURLY_NOTIFICATION_TIME = time.time()
        
    # 13. オープン注文の監視
    await open_order_management_loop()
    
    # 14. ループ実行時間の調整
    end_time = time.time()
    elapsed_time = end_time - start_time
    time_to_wait = LOOP_INTERVAL - elapsed_time
    
    logging.info(f"✅ BOT LOOP END. 実行時間: {elapsed_time:.2f}秒。")
    
    if time_to_wait > 0:
        logging.info(f"😴 次のループまで {time_to_wait:.2f}秒待機します。")
        await asyncio.sleep(time_to_wait)
    else:
        logging.warning("⚠️ BOT LOOPの実行時間が長すぎます。レートリミットに注意してください。")


async def send_telegram_notification(message: str):
    """Telegramに通知を送信する"""
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
        # HTTPリクエストは非ブロッキングな asyncio.to_thread を使用して実行 (Python 3.7以降)
        # requests.post(url, data=payload, timeout=10) # ブロッキング版
        # 外部ライブラリのasyncio-requestsなどを使わず、requestsとto_threadで対応
        
        await asyncio.to_thread(requests.post, url, data=payload, timeout=10)
        logging.info("✅ Telegram通知を送信しました。")
        
    except Exception as e:
        logging.error(f"❌ Telegram通知の送信に失敗: {e}")


# ====================================================================================
# STARTUP & MAIN
# ====================================================================================

# FastAPIインスタンスの作成
app = FastAPI()

async def main():
    """ボットの起動とメインループの実行"""
    
    # CCXTクライアントの初期化
    await initialize_client()
    
    # 初期マクロコンテキストの取得 (初回の取引閾値決定のため)
    global GLOBAL_MACRO_CONTEXT
    GLOBAL_MACRO_CONTEXT = await fetch_fgi_data()
    
    # 初回のトップシンボルリストを取得
    global CURRENT_MONITOR_SYMBOLS, LAST_SUCCESS_TIME
    CURRENT_MONITOR_SYMBOLS = await fetch_top_symbols()
    LAST_SUCCESS_TIME = time.time()
    
    # メインBOTループの開始
    while True:
        try:
            await main_bot_loop()
            
        except Exception as e:
            # 致命的なエラーが発生した場合の処理
            logging.critical(f"🚨 メインBOTループで致命的なエラーが発生: {e}", exc_info=True)
            
            # Telegramでエラー通知
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
    
    # USDT以外の保有資産は fetch_account_status で取得されたものを再利用すべきだが、ここでは簡易化
    # 正確な最新情報はメインループでのみ更新されるため、古い可能性がある
    
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
        "last_signals": [{'symbol': s['symbol'], 'score': f"{s['score']*100:.2f}", 'timeframe': s['timeframe']} for s in LAST_ANALYSIS_SIGNALS[:5]],
        "open_positions_list": [{'symbol': p['symbol'], 'entry': format_price_precision(p['entry_price']), 'sl_id': p['sl_order_id'], 'tp_id': p['tp_order_id']} for p in OPEN_POSITIONS],
    }
    
    return JSONResponse(content=status_data)

@app.on_event("startup")
async def startup_event():
    """FastAPIの起動時にメインボットを実行する"""
    # メインボットを別タスクとして実行
    asyncio.create_task(main())

# サーバー実行
if __name__ == "__main__":
    # main関数はFastAPIのイベントで実行されるため、ここではUvicornを起動する
    # main_render.pyという名前なので、モジュール名は main_render
    uvicorn.run("main_render:app", host="0.0.0.0", port=8000, log_level="info")
