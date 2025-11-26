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
        f"  - **有効シグナル**: <code>{analyzed_count}</code> 件 (スコア > {0.50*100:.0f})\n" # ベーススコア以上
        f"  - **スキップ**: <code>{skipped_count}</code> 銘柄 ({'、'.join(attempt_log.values()) if attempt_log else 'なし'})\n"
        f"  - **現在の取引閾値**: <code>{current_threshold*100:.0f} / 100</code>\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
    )
    
    if signals_sorted:
        best_signal = signals_sorted[0]
        
        message += (
            f"\n"
            f"🥇 **ベストスコア銘柄 (High)**\n"
            f"  - **銘柄**: <b>{best_signal['symbol']}</b> ({best_signal['timeframe']})\n"
            f"  - **スコア**: <code>{best_signal['score'] * 100:.2f} / 100</code>\n"
            f"  - **推定勝率**: <code>{get_estimated_win_rate(best_signal['score'])}</code>\n"
            f"  - **エントリー (Entry)**: <code>{format_price_precision(best_signal['entry_price'])}</code>\n"
            f"  - **SL/TP**: <code>{format_price_precision(best_signal['stop_loss'])}</code> / <code>{format_price_precision(best_signal['take_profit'])}</code>\n"
            f"  - **R:R**: <code>1:{best_signal['rr_ratio']:.2f}</code>\n"
        )
        
        # ワーストスコアは、signals_sortedの中でスコアが0.50より大きいものの中から選ぶ
        worst_signal_index = next((i for i, s in enumerate(signals_sorted) if s['score'] > 0.50), len(signals_sorted) - 1)
        worst_signal = signals_sorted[-1]
        
        # 最低スコアが最高スコアと同じでない、かつ有効なシグナルが2つ以上ある場合
        if worst_signal['score'] < best_signal['score'] and analyzed_count >= 2:
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
            
    else:
        message += f"\n➖ **有効なシグナルは見つかりませんでした**\n\n"
        
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
        'trade_result': signal.get('trade_result'), # 取引結果 (成功/失敗) を含む
        'tech_data': _to_json_compatible(signal.get('tech_data', {})) # テクニカルデータを標準Python型に変換
    }
    
    try:
        # ログをJSONファイルに追記
        log_file = 'apex_bot_signals.json'
        with open(log_file, 'a') as f:
            f.write(json.dumps(log_data) + '\n')
    except Exception as e:
        logging.error(f"❌ シグナルログの書き込みに失敗しました: {e}")


# ====================================================================================
# CCXT & EXCHANGE CLIENT 
# ====================================================================================

async def initialize_exchange_client():
    """CCXTクライアントを初期化する"""
    global EXCHANGE_CLIENT, IS_CLIENT_READY
    
    if IS_CLIENT_READY:
        return
        
    logging.info(f"💡 CCXTクライアント ({CCXT_CLIENT_NAME.upper()}) の初期化を開始します...")

    # CCXTの取引所クラスを動的に取得
    exchange_class = getattr(ccxt_async, CCXT_CLIENT_NAME.lower(), None)
    
    if exchange_class is None:
        logging.critical(f"🚨 サポートされていない取引所です: {CCXT_CLIENT_NAME}")
        return

    # クライアントインスタンスの作成
    EXCHANGE_CLIENT = exchange_class({
        'apiKey': API_KEY,
        'secret': SECRET_KEY,
        'enableRateLimit': True, # レート制限を有効にする
        'timeout': 30000, # タイムアウトを30秒に設定
        # MEXC specific settings, if needed
        # 'options': { ... }
    })
    
    try:
        # マーケットデータをロード (初回のみ)
        await EXCHANGE_CLIENT.load_markets()
        IS_CLIENT_READY = True
        logging.info(f"✅ CCXTクライアント ({CCXT_CLIENT_NAME.upper()}) の初期化に成功しました。")
        
    except Exception as e:
        logging.critical(f"🚨 CCXTクライアントの初期化またはマーケットデータのロードに失敗しました: {e}", exc_info=True)
        IS_CLIENT_READY = False
        await EXCHANGE_CLIENT.close() # 失敗時はクローズ

async def telegram_send_message(message: str):
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
    open_ccxt_positions = [] # CCXTが認識している保有資産 (ボット管理外を含む)

    try:
        # 1. 口座残高の取得
        balance = await EXCHANGE_CLIENT.fetch_balance()
        
        # 2. 利用可能なUSDT残高 (取引に使用可能な残高)
        total_usdt_balance = balance.get('free', {}).get('USDT', 0.0)
        
        # 3. 総資産額 (Equity) の計算
        # USDT残高をまずEquityに加算
        total_equity += balance.get('total', {}).get('USDT', 0.0)
        
        # その他の保有資産（BTC, ETHなど）の評価額をUSDT建てで加算
        for currency, amount in balance.get('total', {}).items():
            if currency not in ['USDT', 'USD'] and amount is not None and amount > 0.000001:
                try:
                    # シンボル形式に変換 (例: BTC/USDT)
                    symbol = f"{currency}/USDT"
                    
                    # Tickerを取得してUSDT建ての価格を調べる
                    ticker = await EXCHANGE_CLIENT.fetch_ticker(symbol)
                    current_price = ticker['last']
                    usdt_value = amount * current_price
                    
                    total_equity += usdt_value
                    
                    # ポジションリストに追加
                    open_ccxt_positions.append({
                        'symbol': symbol,
                        'base_currency': currency,
                        'amount': amount,
                        'usdt_value': usdt_value,
                        'current_price': current_price
                    })
                    
                except Exception as e:
                    # 取引所がそのシンボルを持っていない可能性など
                    logging.warning(f"⚠️ {currency} のUSDT評価額取得に失敗しました: {e}")
                    
        GLOBAL_TOTAL_EQUITY = total_equity # グローバル変数を更新
        
        # 4. 結果を返す
        return {
            'total_usdt_balance': total_usdt_balance,
            'total_equity': total_equity,
            'open_positions': open_ccxt_positions,
            'error': False
        }

    except Exception as e:
        logging.critical(f"🚨 口座ステータスの取得中にCCXTエラーが発生しました: {e}", exc_info=True)
        return {'total_usdt_balance': 0.0, 'total_equity': 0.0, 'open_positions': [], 'error': True}

async def update_monitor_symbols() -> List[str]:
    """
    取引所の出来高上位銘柄を取得し、監視対象リストを更新する。
    デフォルトの銘柄も保持し、出来高の少ない銘柄が選出されないようにする。
    """
    global EXCHANGE_CLIENT
    
    if SKIP_MARKET_UPDATE:
        logging.info("ℹ️ 環境設定により、出来高上位銘柄の更新をスキップし、デフォルト銘柄を使用します。")
        return DEFAULT_SYMBOLS.copy()
        
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.critical("🚨 出来高上位銘柄の取得に失敗しました。クライアントが準備できていません。")
        return DEFAULT_SYMBOLS.copy()

    logging.info("💡 出来高上位銘柄 (TOP_SYMBOL_LIMIT) の更新を試みます...")
    
    try:
        # すべてのティッカー情報を取得
        tickers = await EXCHANGE_CLIENT.fetch_tickers()
        
        # USDTペアのみを抽出し、24時間出来高 (quoteVolume) でソートする
        usdt_tickers = {}
        for symbol, ticker in tickers.items():
            if '/USDT' in symbol and ticker.get('quoteVolume') is not None and ticker['quoteVolume'] > 0:
                usdt_tickers[symbol] = ticker['quoteVolume']

        # 出来高降順でソート
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
    # CCXTでUSDTペアが存在しないため、ここでは外部APIを使用する (例としてBinance Klinesを使う)
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
        logging.error(f"❌ FGIデータ取得失敗: {e}。デフォルト値を使用します。")
        
    # 2. 為替データ (USDX代替) の取得とボーナスの計算
    try:
        # USD/JPY (またはUSDC/JPY) の直近50時間のデータでトレンドを分析
        # 目的: ドル高 (リスクオフ/暗号通貨不利) or ドル安 (リスクオン/暗号通貨有利) を判断する
        response = await asyncio.to_thread(requests.get, FOREX_API_URL, timeout=5)
        klines = response.json()
        
        if klines and len(klines) >= 50:
            forex_df = pd.DataFrame(klines, columns=['time', 'open', 'high', 'low', 'close', 'volume', 'close_time', 'quote_volume', 'trades', 'buy_base_volume', 'buy_quote_volume', 'ignore'])
            forex_df['close'] = pd.to_numeric(forex_df['close'])
            
            # 直近の終値と50期間SMAを比較
            sma_50 = forex_df['close'].rolling(window=50).mean().iloc[-1]
            last_close = forex_df['close'].iloc[-1]
            
            # 乖離率の計算
            deviation = (last_close - sma_50) / sma_50
            
            # ドル安 (deviation < 0) は暗号通貨に有利 (+ボーナス)
            # ドル高 (deviation > 0) は暗号通貨に不利 (-ペナルティ)
            # 最大ボーナス/ペナルティを FGI_PROXY_BONUS_MAX に制限
            
            # 乖離率が-0.5% (0.005) で最大ボーナス、+0.5% (0.005) で最大ペナルティとする
            MAX_DEVIATION = 0.005
            
            if abs(deviation) > MAX_DEVIATION:
                # 最大ボーナス/ペナルティを適用
                factor = FGI_PROXY_BONUS_MAX if deviation < 0 else -FGI_PROXY_BONUS_MAX
                forex_bonus = factor
                
            elif deviation != 0:
                # 線形にボーナス/ペナルティを適用
                forex_bonus = - (deviation / MAX_DEVIATION) * FGI_PROXY_BONUS_MAX
            
            # logging.info(f"✅ 為替データ取得成功: 終値={last_close:.4f}, SMA50={sma_50:.4f}, Deviation={deviation*100:.2f}%, Bonus={forex_bonus*100:.2f}点")
            
    except Exception as e:
        logging.error(f"❌ 為替データ取得失敗: {e}。デフォルト値を使用します。")
        
    # 3. 結果を返す
    return {
        'fgi_proxy': fgi_proxy,
        'fgi_raw_value': fgi_raw_value,
        'forex_bonus': forex_bonus
    }

# ====================================================================================
# TRADING LOGIC - TECHNICAL ANALYSIS & SCORING
# ====================================================================================

def calculate_technical_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """ Pandas DataFrameにテクニカル指標を追加する """
    
    # 終値が float であることを確認 (ccxtのOHLCVは数値だが、念のため)
    df['close'] = pd.to_numeric(df['close'])
    df['high'] = pd.to_numeric(df['high'])
    df['low'] = pd.to_numeric(df['low'])
    df['volume'] = pd.to_numeric(df['volume'])

    # Simple Moving Averages (SMA)
    df['SMA_50'] = ta.sma(df['close'], length=50)
    df['SMA_200'] = ta.sma(df['close'], length=LONG_TERM_SMA_LENGTH) # 長期トレンド用

    # Relative Strength Index (RSI) - 14期間
    df['RSI'] = ta.rsi(df['close'], length=14)

    # Moving Average Convergence Divergence (MACD) - 12, 26, 9
    macd_data = ta.macd(df['close'], fast=12, slow=26, signal=9, append=False)
    
    if macd_data is not None and not macd_data.empty and len(macd_data.columns) >= 3:
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
    if bb_data is not None and not bb_data.empty:
        # 動的にキーを特定するために、'BBL', 'BBU', 'BBM' で始まるキーを探す
        bb_lower_key = next((col for col in bb_data.columns if col.startswith('BBL')), None)
        bb_upper_key = next((col for col in bb_data.columns if col.startswith('BBU')), None)
        bb_middle_key = next((col for col in bb_data.columns if col.startswith('BBM')), None)
        
        if bb_lower_key and bb_upper_key and bb_middle_key:
            df['BBL'] = bb_data[bb_lower_key]
            df['BBU'] = bb_data[bb_upper_key]
            df['BBM'] = bb_data[bb_middle_key]
        else:
            logging.error("❌ BBANDSのキーを特定できませんでした。")
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
    
    # Volume Change (出来高変化率) - 過去5期間の平均との比較
    df['Volume_Avg_5'] = df['volume'].rolling(window=5).mean()
    df['Volume_Change'] = (df['volume'] / df['Volume_Avg_5']) - 1.0 

    return df

def calculate_rr_ratio(price: float, sl: float, tp: float) -> float:
    """ リスクリワード比 (R:R) を計算する """
    risk = abs(price - sl)
    reward = abs(tp - price)
    
    if risk == 0:
        return 0.0 # リスクがゼロの場合は無限大だが、0.0として扱う
    
    return reward / risk

def calculate_stop_loss_take_profit(current_price: float, atr_value: float) -> Tuple[float, float, float]:
    """ 
    現在の価格とATR値に基づいて、SL/TPの価格とR:Rターゲットを計算する。
    ここでは、固定のリスクリワード1:2を目標とし、SLを1.5 * ATRに設定する。
    """
    
    if atr_value <= 0:
        return current_price * 0.99, current_price * 1.01, 1.0 # フォールバック

    # 1. リスクリワードターゲット
    RR_TARGET = 2.0 
    
    # 2. ストップロス (SL) の計算: 現在価格 - 1.5 * ATR
    # 価格変動の1.5倍のリスクを許容
    sl_distance = 1.5 * atr_value
    stop_loss = current_price - sl_distance
    
    # 3. テイクプロフィット (TP) の計算: SL距離 * RR比
    tp_distance = sl_distance * RR_TARGET
    take_profit = current_price + tp_distance

    # 4. 実際のR:R比を計算
    rr_ratio = calculate_rr_ratio(current_price, stop_loss, take_profit)

    return stop_loss, take_profit, rr_ratio

def score_signal(df: pd.DataFrame, timeframe: str, market_ticker: Dict, macro_context: Dict) -> Optional[Dict]:
    """
    分析されたOHLCVデータとテクニカル指標に基づいて取引スコアを計算する (ロングシグナルのみ)。
    
    Args:
        df (pd.DataFrame): テクニカル指標が追加されたOHLCVデータ
        timeframe (str): タイムフレーム (例: '1h')
        market_ticker (Dict): 最新のティッカー情報
        macro_context (Dict): FGIや為替などのマクロ環境データ
        
    Returns:
        Optional[Dict]: シグナルデータ (score, sl, tpなど) または None
    """
    if df.empty or len(df) < LONG_TERM_SMA_LENGTH:
        logging.warning(f"⚠️ {market_ticker['symbol']} ({timeframe}): データ不足のためスキップします。")
        return None
        
    last_candle = df.iloc[-1]
    current_price = market_ticker['last']
    
    # ATRがない場合や異常な値の場合はスキップ
    if np.isnan(last_candle['ATR']) or last_candle['ATR'] <= 0:
        logging.warning(f"⚠️ {market_ticker['symbol']} ({timeframe}): ATR値が不正なためスキップします。")
        return None

    # SL/TPの計算
    stop_loss, take_profit, rr_target = calculate_stop_loss_take_profit(current_price, last_candle['ATR'])
    
    # エントリー価格 (指値価格) を現在の価格と同じに設定 (IOC注文を想定)
    entry_price = current_price
    
    # リスクがゼロまたはマイナスになる場合はスキップ (通常ありえないが防御的プログラミング)
    if stop_loss >= entry_price or take_profit <= entry_price:
        # logging.warning(f"⚠️ {market_ticker['symbol']} ({timeframe}): SL/TP設定が不正なためスキップします。SL={stop_loss:.4f}, Entry={entry_price:.4f}, TP={take_profit:.4f}")
        return None
        
    total_score = 0.0
    tech_data = {} # スコア詳細格納用

    # ====================================================================
    # SCORING COMPONENTS (ロングシグナルを想定)
    # ====================================================================

    # 1. トレンド/価格位置の確認
    is_above_long_term_sma = last_candle['close'] > last_candle['SMA_200']
    
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
    
    # 過去3期間の安値の最小値を取得
    low_min_3 = df['low'].iloc[-4:-1].min() # 最後の3期間のlow
    
    # 現在の終値が直近の安値の最小値よりも上にある
    if last_candle['close'] > low_min_3:
        structural_pivot_bonus = STRUCTURAL_PIVOT_BONUS
        
    total_score += structural_pivot_bonus
    tech_data['structural_pivot_bonus'] = structural_pivot_bonus

    # E. MACDペナルティ (25点)
    # MACDがシグナルラインを下回っている (不利なクロス) か、ヒストグラムがゼロ以下で減少している (発散)
    macd_penalty_value = 0.0
    if not (last_candle['MACD'] > last_candle['MACDs'] and last_candle['MACDh'] > 0): # ゴールデンクロスしてない、またはヒストグラムが0以下でマイナス方向に拡大している
        macd_penalty_value = MACD_CROSS_PENALTY
    
    total_score -= macd_penalty_value
    tech_data['macd_penalty_value'] = macd_penalty_value

    # F. RSIモメンタムボーナス (10点)
    # RSIがRSI_MOMENTUM_LOW (45) 以下で、かつ上昇傾向にある（安値圏からの反転期待）
    rsi_momentum_bonus_value = 0.0
    tech_data['rsi_value'] = last_candle['RSI']

    if last_candle['RSI'] <= RSI_MOMENTUM_LOW: # 45以下でモメンタム候補
        # 3期間前のRSIより高い（直近の上昇モメンタム）
        rsi_3_ago = df['RSI'].iloc[-4]
        if last_candle['RSI'] > rsi_3_ago:
            # 50までの距離に応じてボーナスを線形に増加させる (RSIが低いほど、反転時のボーナスが大きくなる)
            # 例: RSI 30 -> 15 / 20 = 0.75 * MAX_BONUS
            # 例: RSI 45 -> 5 / 20 = 0.25 * MAX_BONUS
            max_distance = 50.0 - (RSI_MOMENTUM_LOW - 5) # 50.0 - 40 = 10 (RSI 40で最小ボーナス)
            if max_distance > 0:
                distance_to_50 = 50.0 - last_candle['RSI']
                ratio = min(distance_to_50 / max_distance, 1.0) # 最大1.0に制限
                rsi_momentum_bonus_value = RSI_MOMENTUM_BONUS_MAX * ratio

    total_score += rsi_momentum_bonus_value
    tech_data['rsi_momentum_bonus_value'] = rsi_momentum_bonus_value

    # G. OBVモメンタム確証ボーナス (5点)
    # OBVが直近のN期間（例: 5期間）で上昇傾向にある
    obv_momentum_bonus_value = 0.0
    
    # OBVの5期間SMAを取得
    obv_sma_5 = df['OBV'].rolling(window=5).mean().iloc[-1]
    
    # 直近のOBVがSMAを上回っている
    if last_candle['OBV'] > obv_sma_5:
        obv_momentum_bonus_value = OBV_MOMENTUM_BONUS
        
    total_score += obv_momentum_bonus_value
    tech_data['obv_momentum_bonus_value'] = obv_momentum_bonus_value

    # H. 出来高スパイクボーナス (7点)
    # 出来高が過去5期間の平均を大きく上回っている（例: 50%以上）
    volume_increase_bonus_value = 0.0
    if last_candle['Volume_Change'] > 0.50: # 50%以上の増加
        volume_increase_bonus_value = VOLUME_INCREASE_BONUS
        
    total_score += volume_increase_bonus_value
    tech_data['volume_increase_bonus_value'] = volume_increase_bonus_value
    
    # I. 流動性/板の厚みボーナス (7点)
    # ATR/価格でボラティリティを計測し、ボラティリティが低すぎず（取引機会）、高すぎない（安定性）
    # より簡略化し、単に出来高の大きさに比例させる (出来高上位銘柄が優位になる)
    liquidity_bonus_value = 0.0
    
    # 24H Quote Volume (USDT出来高) を正規化して使用
    # 出来高の絶対値に基づいてスコアを付与 (対数スケールで計算)
    try:
        quote_volume = market_ticker['quoteVolume']
        if quote_volume > 0:
            # log10(volume) を使用して、ボリュームの大きさに応じて線形にスコアを付与
            # 例: 1,000,000 (log6) から 1,000,000,000 (log9) の範囲で正規化
            # 最小出来高を10^6 (1M)、最大出来高を10^9 (1B) と想定
            min_log = 6.0
            max_log = 9.0
            log_volume = math.log10(quote_volume)
            
            # log_volumeを0から1に正規化
            if log_volume <= min_log:
                ratio = 0.0
            elif log_volume >= max_log:
                ratio = 1.0
            else:
                ratio = (log_volume - min_log) / (max_log - min_log)
                
            liquidity_bonus_value = LIQUIDITY_BONUS_MAX * ratio
            
    except Exception:
        # volume情報がない場合はボーナスなし
        pass
        
    total_score += liquidity_bonus_value
    tech_data['liquidity_bonus_value'] = liquidity_bonus_value
    
    # J. ボラティリティペナルティ (低ボラティリティ)
    volatility_penalty_value = 0.0
    # BB幅 / BBM (終値のSMA) の比率が低すぎる場合 (例: 1%未満)
    if not np.isnan(last_candle['BBL']) and not np.isnan(last_candle['BBU']) and last_candle['BBM'] > 0:
        bb_width_ratio = (last_candle['BBU'] - last_candle['BBL']) / last_candle['BBM']
        if bb_width_ratio < VOLATILITY_BB_PENALTY_THRESHOLD: # 例: 1%未満
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

async def fetch_ohlcv_and_analyze(symbol: str, tf: str, limit: int, market_ticker: dict, macro_context: Dict) -> Optional[Dict]:
    """ 
    OHLCVデータを取得し、テクニカル分析とスコアリングを実行する 
    """
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        return None
        
    try:
        # OHLCVデータを取得
        ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(symbol, tf, limit=limit)
        
        if len(ohlcv) < limit:
            # logging.warning(f"⚠️ {symbol} ({tf}): 必要なデータ数 ({limit}) を取得できませんでした ({len(ohlcv)})。スキップします。")
            return None

        # DataFrameに変換
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        
        # テクニカル指標の計算
        df = calculate_technical_indicators(df)
        
        # スコアリング
        signal = score_signal(df, tf, market_ticker, macro_context)
        
        return signal
        
    except ccxt.ExchangeError as e:
        # logging.error(f"❌ 取引所エラー ({symbol} / {tf}): {e}")
        return None
    except Exception as e:
        # logging.error(f"❌ {symbol} ({tf}) の分析中に予期せぬエラーが発生: {e}")
        return None

# ====================================================================================
# TRADING LOGIC - ORDER MANAGEMENT
# ====================================================================================

async def get_dynamic_lot_size(score: float, current_usdt_balance: float) -> Tuple[float, float]:
    """ 
    スコアと現在のUSDT残高に基づいて、動的なロットサイズを計算する。
    
    Args:
        score (float): 取引シグナルスコア (0.0 - 1.0)
        current_usdt_balance (float): 現在の利用可能USDT残高
        
    Returns:
        Tuple[float, float]: (ロットサイズUSDT, ロットサイズ割合)
    """
    global GLOBAL_TOTAL_EQUITY, BASE_TRADE_SIZE_USDT, DYNAMIC_LOT_MIN_PERCENT, DYNAMIC_LOT_MAX_PERCENT, DYNAMIC_LOT_SCORE_MAX
    
    # 1. ベースロットサイズ (最小値保証)
    min_lot_usdt = BASE_TRADE_SIZE_USDT
    
    # 2. 総資産ベースのロットサイズ (動的ロット)
    if GLOBAL_TOTAL_EQUITY > 0:
        # 最小ロット (総資産のX%)
        min_dynamic_lot = GLOBAL_TOTAL_EQUITY * DYNAMIC_LOT_MIN_PERCENT
        # 最大ロット (総資産のY%)
        max_dynamic_lot = GLOBAL_TOTAL_EQUITY * DYNAMIC_LOT_MAX_PERCENT
        
        # スコアに基づいて、最小ロットから最大ロットの間で線形補間
        # DYNAMIC_LOT_SCORE_MAX (例: 0.96) で最大ロットが適用される
        score_base = 0.80 # 80点以下は最低ロット (BASE_TRADE_SIZE_USDTまたはmin_dynamic_lot)
        
        if score < score_base:
            dynamic_lot_usdt = min_dynamic_lot
        elif score >= DYNAMIC_LOT_SCORE_MAX:
            dynamic_lot_usdt = max_dynamic_lot
        else:
            # 線形補間
            ratio = (score - score_base) / (DYNAMIC_LOT_SCORE_MAX - score_base)
            dynamic_lot_usdt = min_dynamic_lot + (max_dynamic_lot - min_dynamic_lot) * ratio
            
        # 3. 最終ロットの決定
        # 少なくとも BASE_TRADE_SIZE_USDT は確保
        final_lot_usdt = max(min_lot_usdt, dynamic_lot_usdt)
    
    else:
        # 総資産情報がない場合、BASE_TRADE_SIZE_USDT を使用
        final_lot_usdt = min_lot_usdt
    
    # 4. 利用可能残高による制限
    # 注文に使用できるのは、利用可能残高の最大80%までとする (手数料や変動分を考慮)
    max_available_lot = current_usdt_balance * 0.80
    
    # 最終決定
    final_lot_usdt = min(final_lot_usdt, max_available_lot)
    
    # 5. ロットサイズの割合 (表示用)
    lot_percent = (final_lot_usdt / GLOBAL_TOTAL_EQUITY) * 100 if GLOBAL_TOTAL_EQUITY > 0 else 0.0
    
    return final_lot_usdt, lot_percent

async def adjust_order_amount(symbol: str, usdt_amount: float, price: float) -> Tuple[float, float]:
    """
    USDT建ての想定金額と価格から、取引所の精度要件を満たすベース通貨の数量を計算し、丸める。
    
    Args:
        symbol (str): 通貨ペア (例: BTC/USDT)
        usdt_amount (float): 注文したいUSDT建ての金額
        price (float): 注文価格 (指値または現在の価格)
        
    Returns:
        Tuple[float, float]: (丸められたベース通貨数量, 最終的なUSDT金額)
    """
    global EXCHANGE_CLIENT
    
    if symbol not in EXCHANGE_CLIENT.markets:
        return 0.0, 0.0
        
    market = EXCHANGE_CLIENT.markets[symbol]
    
    # 1. 注文数量の計算 (ベース通貨建て)
    base_amount = usdt_amount / price
    
    # 2. 数量の精度要件 (amount precision)
    precision = market['precision']['amount']
    
    # 3. 最小注文数量 (minAmount)
    min_amount = market['limits']['amount']['min']

    # 4. 数量の丸め
    if precision is None:
        # 精度が設定されていない場合は、一旦小数点以下4桁としておく
        precision_digits = 4
    elif isinstance(precision, float) and precision < 1:
        # 例: 0.0001
        try:
            precision_digits = max(0, int(-math.log10(precision)))
        except ValueError: # math.log10(0) を避ける
            precision_digits = 8 
    elif isinstance(precision, int):
        # 例: 4 (小数第4位)
        precision_digits = precision
    else:
        precision_digits = 4

    # 精度桁数で丸め（四捨五入）
    if precision_digits > 0:
        # 0.5を加算してfloorすることで四捨五入に近い動作を実現
        base_amount_rounded = math.floor(base_amount * (10**precision_digits)) / (10**precision_digits)
        # base_amount_rounded = round(base_amount, precision_digits) # 通常はこれを使用するが、取引所によっては切り捨てを求めるため、安全を見てfloorを使用
    else:
        # 整数に丸め
        base_amount_rounded = math.floor(base_amount)
        
    # 5. 最小注文数量のチェック
    if base_amount_rounded < min_amount:
        logging.warning(f"⚠️ {symbol}: 計算数量 ({base_amount_rounded:.8f}) が最小要件 ({min_amount:.8f}) を満たしません。")
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
    
    取引所APIによっては、現物のSL/TP同時注文 (Take Profit/Stop Loss order) をサポートしていない場合がある。
    その場合、`create_order` または `create_orders` で `stop_loss` と `take_profit` を指定するか、
    CCXTが対応している場合は、`create_stop_loss_order` や `create_take_profit_order` を使用する。
    
    ここでは、一般的なMEXC現物取引を想定し、トリガー価格を指定した**ストップリミット/ストップマーケット**注文を使用する。
    
    Returns: 
        {'status': 'ok', 'sl_order_id': '...', 'tp_order_id': '...', 'filled_amount': amount, 'filled_usdt': usdt}
        または
        {'status': 'error', 'error_message': '...'}
    """
    global EXCHANGE_CLIENT, IS_CLIENT_READY
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        return {'status': 'error', 'error_message': 'クライアントが準備できていません'}
        
    if filled_amount <= 0:
         return {'status': 'error', 'error_message': '約定数量がゼロ以下です'}

    logging.info(f"💡 SL/TP注文を設定します: {symbol} (Qty: {filled_amount:.4f}, SL: {format_price_precision(stop_loss)}, TP: {format_price_precision(take_profit)})")

    # 1. 共通設定: 数量はポジションの保有数量
    amount = filled_amount
    
    # 2. TP注文 (テイクプロフィット - 利益確定) の設定
    tp_order_id = None
    try:
        # TPは指値注文 (Limit Order) を使用することが多い。
        # または、ストップリミット/ストップマーケット注文で、TP価格をトリガーとして成行売り/指値売りを行う。
        # ここでは、簡潔のため、トリガー価格 = 指値価格 としてストップリミットを試みる
        
        # TPトリガー価格 (テイクプロフィット価格)
        tp_trigger_price = take_profit
        # TP指値価格 (約定価格) - トリガー価格より少し低く設定して約定確率を高める
        tp_limit_price = take_profit * 0.999 
        
        # ストップリミット注文: 価格がtp_trigger_priceに達したらtp_limit_priceで売る
        # CCXTの標準メソッドは `create_order` で stopLossPrice/takeProfitPrice を渡す形式が多いが、
        # MEXCなどの現物取引では `create_stop_limit_order` や `create_take_profit_order` が必要。
        # CCXTの抽象化に頼らず、ネイティブなパラメータで実現可能な `create_order` の拡張を使用
        
        # ccxtは`type='take_profit_limit'`や`type='stop_loss_limit'`に対応している場合がある
        if 'take_profit_limit' in EXCHANGE_CLIENT.market(symbol)['info'].get('options', {}).get('default_allowed_orders', []):
            tp_order = await EXCHANGE_CLIENT.create_order(
                symbol=symbol,
                type='take_profit_limit', # CCXT標準のTP指値
                side='sell',
                amount=amount,
                price=tp_limit_price, # 注文価格
                params={'stopPrice': tp_trigger_price} # トリガー価格
            )
        else:
             # fall back to standard limit order if exchange does not support TP/SL
             tp_order = await EXCHANGE_CLIENT.create_order(
                 symbol=symbol,
                 type='limit', # 通常の指値注文
                 side='sell',
                 amount=amount,
                 price=take_profit, # TP価格で指値売り
                 params={}
             )
        
        tp_order_id = tp_order['id']
        logging.info(f"✅ TP注文成功 (指値): ID={tp_order_id}, Price={format_price_precision(take_profit)}")
        
    except Exception as e:
        logging.critical(f"🚨 TP注文失敗 ({symbol}): {e}", exc_info=True)
        # TP注文失敗はSL注文に影響しないため、続行
        pass # エラーメッセージはSL注文の失敗と合わせて最後に処理する

    # 3. SL注文 (ストップロス - 損切り) の設定
    sl_order_id = None
    sl_trigger_price = stop_loss
    sl_limit_price = stop_loss * 0.999 # SL指値価格 (トリガー価格より少し低く設定して約定確率を高める)
    
    try:
        # ストップリミット注文: 価格がsl_trigger_priceに達したらsl_limit_priceで売る
        if 'stop_loss_limit' in EXCHANGE_CLIENT.market(symbol)['info'].get('options', {}).get('default_allowed_orders', []):
            sl_order = await EXCHANGE_CLIENT.create_order(
                symbol=symbol,
                type='stop_loss_limit', # CCXT標準のSL指値
                side='sell',
                amount=amount,
                price=sl_limit_price, # 注文価格
                params={'stopPrice': sl_trigger_price} # トリガー価格
            )
        else:
            # fall back to standard order with stop loss parameter if supported
            sl_order = await EXCHANGE_CLIENT.create_order(
                symbol=symbol,
                type='limit', # 通常の指値注文 (トリガー機能がない場合)
                side='sell',
                amount=amount,
                price=stop_loss,
                params={}
            )
            
        sl_order_id = sl_order['id']
        logging.info(f"✅ SL注文成功 (ストップ): ID={sl_order_id}, Trigger Price={format_price_precision(sl_trigger_price)}")
        
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

    # 4. 成功時のリターン
    return {
        'status': 'ok',
        'sl_order_id': sl_order_id,
        'tp_order_id': tp_order_id,
        'filled_amount': filled_amount,
        # TP価格で概算のUSDT額を返す（エントリー価格ではないが、後続処理で再計算するため問題なし）
        'filled_usdt': filled_amount * take_profit 
    }

async def close_position_immediately(symbol: str, amount: float) -> Dict:
    """ 不完全なポジションを成行売りで即座にクローズする (リカバリ用) """
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY or amount <= 0:
        return {'status': 'skipped', 'error_message': 'スキップ: クライアント未準備または数量がゼロです。'}

    logging.warning(f"🚨 不完全ポジションの強制クローズを試みます: {symbol} (数量: {amount:.4f})")

    try:
        # 数量の丸め（成行注文でも精度は重要）
        # 注文数量を正確に計算する必要がある。ここでは、おおよそ現在の価格でUSDT額を計算
        market = EXCHANGE_CLIENT.markets[symbol]
        ticker = await EXCHANGE_CLIENT.fetch_ticker(symbol)
        current_price = ticker['last']
        
        # amount はベース通貨量 (例: BTC)
        # adjust_order_amountはUSDT額からベース通貨量を計算するので、ここでは使用せず、
        # CCXTの `amount_to_precision` を使用する
        
        # CCXTの amount_to_precision を使用して丸める
        try:
            base_amount_rounded = EXCHANGE_CLIENT.amount_to_precision(symbol, amount)
        except Exception:
            # CCXTのメソッドが使えない場合は、独自の丸め処理にフォールバック
            base_amount_rounded, _ = await adjust_order_amount(symbol, amount * current_price * 1.0, current_price) 
            
        amount = float(base_amount_rounded)
        
        if amount == 0:
             return {'status': 'skipped', 'error_message': 'スキップ: 丸め後の数量がゼロです。'}

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
            logging.info(f"✅ 強制クローズ成功: {symbol} (約定数量: {closed_amount:.4f})")
            return {
                'status': 'ok', 
                'closed_amount': closed_amount, 
                'exit_price': close_order.get('price', current_price) # 成行注文の平均価格または現在の価格
            }
        else:
            logging.warning(f"⚠️ 強制クローズ失敗: 約定数量がゼロです。")
            return {'status': 'error', 'error_message': '約定数量がゼロです。', 'closed_amount': 0.0}

    except ccxt.ExchangeError as e:
        error_message = f"取引所エラー: {e}"
        logging.error(f"❌ 強制クローズ失敗 ({symbol}): {error_message}", exc_info=True)
        return {'status': 'error', 'error_message': error_message, 'closed_amount': 0.0}
    except Exception as e:
        error_message = f"システムエラー: {e}"
        logging.error(f"❌ 強制クローズ失敗 ({symbol}): {error_message}", exc_info=True)
        return {'status': 'error', 'error_message': error_message, 'closed_amount': 0.0}


async def execute_trade(signal: Dict, lot_size_usdt: float) -> Dict:
    """ 
    取引シグナルに基づいて、現物指値買い注文 (IOC) とSL/TP注文を実行する。
    
    Args:
        signal (Dict): スコアとSL/TP情報を含むシグナルデータ
        lot_size_usdt (float): 実際に注文するUSDT建てのロットサイズ
        
    Returns:
        Dict: 取引結果 (成功/失敗、約定価格、SL/TP注文IDなど)
    """
    global EXCHANGE_CLIENT
    global OPEN_POSITIONS # <--- この行を追加！
    
    symbol = signal['symbol']
    entry_price = signal['entry_price']
    stop_loss = signal['stop_loss']
    take_profit = signal['take_profit']
    
    if TEST_MODE:
        return {'status': 'error', 'error_message': 'TEST_MODEのため取引はスキップされました', 'close_status': 'skipped'}
        
    if lot_size_usdt <= 0:
         return {'status': 'error', 'error_message': '計算されたロットサイズがゼロ以下です', 'close_status': 'skipped'}

    logging.info(f"💡 取引実行: {symbol} ({signal['timeframe']}) - Lot: {format_usdt(lot_size_usdt)} USDT @ {format_price_precision(entry_price)}")

    try:
        # 1. 注文数量の計算 (取引所の精度に丸める)
        amount, final_usdt_amount = await adjust_order_amount(symbol, lot_size_usdt, entry_price)
        
        if amount == 0.0:
             error_message = f"計算ロットサイズが取引所の最小注文数量を満たしません ({format_usdt(lot_size_usdt)} USDT)。"
             logging.warning(f"⚠️ {symbol}: {error_message}")
             return {'status': 'error', 'error_message': error_message, 'close_status': 'skipped'}


        # 2. 現物指値買い注文 (IOC: Immediate-Or-Cancel) を実行
        # IOC注文は、即座に約定可能な数量だけ約定させ、残りをキャンセルする
        order = await EXCHANGE_CLIENT.create_order(
            symbol=symbol,
            type='limit', # 指値注文
            side='buy',
            amount=amount,
            price=entry_price,
            params={'timeInForce': 'IOC'} # 即時約定しなかったらキャンセル
        )
        
        # 3. 約定結果の確認
        filled_amount = order.get('filled', 0.0)
        filled_usdt = filled_amount * order.get('price', entry_price) # 平均約定価格を使用
        
        # 💡 即時約定が発生した場合
        if filled_amount > 0 and filled_usdt > 0:
            logging.info(f"✅ 指値買い注文 約定成功: {symbol} (Qty: {filled_amount:.4f}, USDT: {format_usdt(filled_usdt)})")

            # 4. SL/TP注文の設定
            sl_tp_result = await place_sl_tp_orders(
                symbol=symbol,
                filled_amount=filled_amount,
                stop_loss=stop_loss,
                take_profit=take_profit
            )
            
            # 5. SL/TP設定成功
            if sl_tp_result['status'] == 'ok':
                # ポジションリストに追加
                new_position = {
                    'id': str(uuid.uuid4()), # ユニークなIDを付与
                    'symbol': symbol,
                    'timeframe': signal['timeframe'],
                    'entry_price': order.get('price', entry_price),
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
                    'entry_price': order.get('price', entry_price),
                    'sl_order_id': sl_tp_result['sl_order_id'],
                    'tp_order_id': sl_tp_result['tp_order_id']
                }
            
            # 6. SL/TP設定失敗 -> ポジションを強制クローズ
            else: 
                error_message = sl_tp_result.get('error_message', 'SL/TP設定中に不明なエラー')
                logging.critical(f"🚨 {symbol}: SL/TP設定失敗。ポジションを強制クローズします。: {error_message}")
                
                close_result = await close_position_immediately(symbol, filled_amount)

                return {
                    'status': 'error',
                    'error_message': f'取引成功後のリカバリー失敗: {error_message}',
                    'close_status': close_result['status'],
                    'closed_amount': close_result.get('closed_amount', 0.0),
                    'close_error_message': close_result.get('error_message'),
                }
        
        # 7. 即時約定しなかった (IOC/FOKでフィルされなかった)
        else: 
            error_message = f"指値買い注文 ({format_price_precision(entry_price)}) が即時約定しませんでした (filled: 0.0)。"
            logging.warning(f"⚠️ {symbol}: {error_message}")
            # IOCなので、残高は減っていないはず
            return {'status': 'error', 'error_message': error_message, 'close_status': 'skipped'}

    except ccxt.NetworkError as e:
        error_message = f"ネットワークエラー: {e}"
        logging.error(f"❌ 取引実行失敗 ({symbol}): {error_message}", exc_info=True)
        return {'status': 'error', 'error_message': error_message, 'close_status': 'skipped'}
    except ccxt.ExchangeError as e:
        error_message = f"取引所エラー: {e}"
        logging.error(f"❌ 取引実行失敗 ({symbol}): {error_message}", exc_info=True)
        
        # 💡 CCXTエラーでも約定している可能性を考慮:
        # このエラーが返された時点で購入が成功し、SL/TP設定が必要な場合は、リカバリーが必要だが、
        # IOC注文の場合、通常は約定しないか、約定した場合は正常なレスポンスが返されるはず。
        # ここでは安全を見て、約定していない前提でスキップとする。
        return {'status': 'error', 'error_message': error_message, 'close_status': 'skipped'}


async def open_order_management_loop():
    """ 
    オープンポジションのSL/TP注文のステータスを監視し、決済が発生したらポジションリストから削除する。
    10秒ごと (MONITOR_INTERVAL) に実行される。
    """
    global EXCHANGE_CLIENT, OPEN_POSITIONS, GLOBAL_TOTAL_EQUITY
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.warning("⚠️ オープン注文監視をスキップ: クライアントが準備できていません。")
        return

    # 処理中にリストが変更されるのを防ぐため、コピーをイテレート
    positions_to_check = OPEN_POSITIONS[:]
    
    # 決済済みポジションのIDを保持するリスト
    closed_position_ids = []

    for position in positions_to_check:
        symbol = position['symbol']
        sl_order_id = position['sl_order_id']
        tp_order_id = position['tp_order_id']
        
        sl_status = None
        tp_status = None
        is_closed = False # ポジションが決済されたかどうか

        try:
            # 1. SL注文のステータスを確認
            sl_status = await EXCHANGE_CLIENT.fetch_order(sl_order_id, symbol)
            
            # 2. TP注文のステータスを確認
            tp_status = await EXCHANGE_CLIENT.fetch_order(tp_order_id, symbol)
            
            # 3. 決済判定
            # SL注文が約定完了 (closed/filled) した場合
            if sl_status and sl_status['status'] in ['closed', 'filled']:
                logging.info(f"🛑 SL約定: {symbol} - SL注文 (ID: {sl_order_id}) が約定しました。")
                is_closed = True
                exit_price = sl_status['average'] or sl_status['price']
                exit_type = 'Stop Loss'
                
            # TP注文が約定完了 (closed/filled) した場合
            elif tp_status and tp_status['status'] in ['closed', 'filled']:
                logging.info(f"🛑 TP約定: {symbol} - TP注文 (ID: {tp_order_id}) が約定しました。")
                is_closed = True
                exit_price = tp_status['average'] or tp_status['price']
                exit_type = 'Take Profit'

        except ccxt.OrderNotFound:
            # 注文IDが見つからない = 取引所側でキャンセルされた、または約定後すぐに削除された可能性
            # ここでは安全を見て、両方の注文がNot Foundで、かつポジション残高がないことを確認する必要があるが、
            # 監視ループの複雑性を避けるため、一旦注文が約定完了したという前提で、ポジションの残高チェックを簡略化する。
            # ただし、注文IDがない場合は、ステップ4の再設定ロジックに任せる。
            pass
        
        except Exception as e:
            logging.error(f"❌ 注文ステータス取得中にエラーが発生 ({symbol}): {e}")
            continue # このポジションの処理をスキップ

        # 4. 決済処理の実行
        if is_closed:
            closed_position_ids.append(position['id'])
            
            # 残った注文をキャンセル
            if exit_type == 'Stop Loss' and tp_status and tp_status['status'] == 'open':
                try:
                    await EXCHANGE_CLIENT.cancel_order(tp_order_id, symbol)
                    logging.info(f"✅ SL約定に伴い、TP注文 (ID: {tp_order_id}) をキャンセルしました。")
                except Exception as e:
                    logging.error(f"❌ TP注文のキャンセル失敗 ({symbol}): {e}")
                    
            elif exit_type == 'Take Profit' and sl_status and sl_status['status'] == 'open':
                try:
                    await EXCHANGE_CLIENT.cancel_order(sl_order_id, symbol)
                    logging.info(f"✅ TP約定に伴い、SL注文 (ID: {sl_order_id}) をキャンセルしました。")
                except Exception as e:
                    logging.error(f"❌ SL注文のキャンセル失敗 ({symbol}): {e}")

            # 損益 (PnL) の計算
            pnl_usdt = (exit_price - position['entry_price']) * position['filled_amount']
            pnl_percent = (exit_price / position['entry_price'] - 1) * 100

            # 口座ステータスを再取得し、最新の総資産を更新
            account_status = await fetch_account_status()
            
            # 通知メッセージを作成し、Telegramで送信
            trade_result = {
                'status': 'closed',
                'exit_type': exit_type,
                'exit_price': exit_price,
                'entry_price': position['entry_price'],
                'filled_amount': position['filled_amount'],
                'pnl_usdt': pnl_usdt,
                'pnl_percent': pnl_percent
            }
            
            # 決済シグナルをログに記録
            log_signal({**position, 'trade_result': trade_result}, "ポジション決済")
            
            # 決済通知
            message = format_telegram_message(position, "ポジション決済", 0.0, trade_result, exit_type)
            await telegram_send_message(message)
            
            # グローバルポジションリストから削除
            # この処理はループ終了後にまとめて行うことで、リストのインデックス問題を避ける

        # ★ 5. SL/TPが片方または両方存在しない場合の再設定ロジック (V19.0.53で追加)
        # 注文が約定完了していない (is_closed == False) かつ、
        # SL注文またはTP注文がオープンでない (オープン注文IDがない or ステータスがオープンではない)
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
                symbol=position['symbol'],
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

    # ループ終了後、決済済みポジションをリストから削除
    OPEN_POSITIONS = [p for p in OPEN_POSITIONS if p['id'] not in closed_position_ids]
    

async def analyze_and_get_signals(symbol: str, market_ticker: Dict, macro_context: Dict) -> List[Dict]:
    """ 
    指定された銘柄のすべてのタイムフレームで分析を実行し、有効なシグナルを返す。
    取引クールダウン中の銘柄は分析をスキップし、ログに記録する。
    """
    global LAST_SIGNAL_TIME, TRADE_SIGNAL_COOLDOWN, HOURLY_ATTEMPT_LOG
    
    signals: List[Dict] = []
    
    # クールダウンチェック: 過去2時間以内に取引シグナルが発火していないか
    if symbol in LAST_SIGNAL_TIME and (time.time() - LAST_SIGNAL_TIME[symbol] < TRADE_SIGNAL_COOLDOWN):
        HOURLY_ATTEMPT_LOG[symbol] = "クールダウン"
        return signals

    for tf in TARGET_TIMEFRAMES:
        limit = REQUIRED_OHLCV_LIMITS[tf]
        try:
            signal = await fetch_ohlcv_and_analyze(symbol, tf, limit, market_ticker, macro_context)
            if signal and signal['score'] >= 0.50: # ベーススコア以上のシグナルのみを返す
                signals.append(signal)
        except Exception as e:
            logging.error(f"❌ {symbol} ({tf}) の分析中にエラーが発生: {e}")
            
    return signals


# ====================================================================================
# MAIN LOOP & API ENDPOINT
# ====================================================================================

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

    start_time = time.time()
    now_jst = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")
    logging.info(f"--- 💡 {now_jst} - BOT LOOP START (M1 Frequency) ---")

    try:
        # 1. FGIデータを取得し、グローバルマクロコンテキストを更新
        GLOBAL_MACRO_CONTEXT = await fetch_fgi_data()
        # FGIの値をスコアリングに反映する準備
        macro_influence_score = (GLOBAL_MACRO_CONTEXT.get('fgi_proxy', 0.0) + GLOBAL_MACRO_CONTEXT.get('forex_bonus', 0.0))
        current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT) # 動的閾値を決定
        
        # 2. 口座ステータスを取得し、新規取引の可否をチェック
        account_status = await fetch_account_status()
        
        if account_status.get('error'):
             logging.critical("🚨 口座ステータスの取得に失敗しました。取引処理をスキップします。")
             # 初回通知が完了していない場合は、失敗メッセージを送信
             if not IS_FIRST_MAIN_LOOP_COMPLETED:
                 await telegram_send_message(format_startup_message(account_status, GLOBAL_MACRO_CONTEXT, len(CURRENT_MONITOR_SYMBOLS), current_threshold, BOT_VERSION))
                 IS_FIRST_MAIN_LOOP_COMPLETED = True # 失敗しても、初回通知は完了したと見なす
             return

        # 3. 監視銘柄リストの更新 (出来高上位銘柄を組み込む)
        CURRENT_MONITOR_SYMBOLS = await update_monitor_symbols()
        
        # 4. 全銘柄のティッカー情報を取得 (並列化は不要、単一APIコールで十分)
        tickers = await EXCHANGE_CLIENT.fetch_tickers()
        
        # 5. すべての銘柄/タイムフレームの分析を非同期で実行
        all_signals: List[Dict] = []
        tasks = []
        HOURLY_ATTEMPT_LOG = {} # 1時間ごとの試行ログをリセット
        
        for symbol in CURRENT_MONITOR_SYMBOLS:
            market_ticker = tickers.get(symbol)
            if market_ticker:
                # ティック価格が取得できない、または有効な市場でない場合はスキップ
                if market_ticker['last'] is None or market_ticker['last'] <= 0 or not market_ticker['active']:
                    HOURLY_ATTEMPT_LOG[symbol] = "価格/市場無効"
                    continue
                    
                # 分析タスクを追加
                tasks.append(
                    analyze_and_get_signals(symbol, market_ticker, GLOBAL_MACRO_CONTEXT)
                )
            else:
                 HOURLY_ATTEMPT_LOG[symbol] = "ティッカーなし"
                 
        
        # すべての分析タスクの完了を待つ
        analysis_results = await asyncio.gather(*tasks)
        
        # 結果を結合 (Noneや空のリストをフィルタリング)
        for result in analysis_results:
            if result:
                all_signals.extend(result)
        
        # スコア降順でソート
        all_signals.sort(key=lambda x: x['score'], reverse=True)
        LAST_ANALYSIS_SIGNALS = all_signals # 最後の分析結果を保存

        # 6. ベストシグナル候補の選定と取引実行
        best_signal: Optional[Dict] = all_signals[0] if all_signals else None
        
        if best_signal:
            logging.info(f"🏆 Best Signal Found: {best_signal['symbol']} ({best_signal['timeframe']}) - Score: {best_signal['score']*100:.2f}")

            # 閾値チェック
            if best_signal['score'] >= current_threshold:
                
                # 新規取引に必要な最小残高チェック
                if account_status['total_usdt_balance'] >= MIN_USDT_BALANCE_FOR_TRADE:
                    
                    # クールダウンチェック (analyze_and_get_signalsでもチェックしているが、最終確認)
                    symbol_cooldown_expired = symbol not in LAST_SIGNAL_TIME or (time.time() - LAST_SIGNAL_TIME[best_signal['symbol']] >= TRADE_SIGNAL_COOLDOWN)
                    
                    # ポジション保有チェック (二重エントリー防止)
                    has_position = any(p['symbol'] == best_signal['symbol'] for p in OPEN_POSITIONS)

                    if symbol_cooldown_expired and not has_position:
                        
                        # 動的ロットサイズの計算
                        lot_size_usdt, _ = await get_dynamic_lot_size(best_signal['score'], account_status['total_usdt_balance'])
                        best_signal['lot_size_usdt'] = lot_size_usdt
                        
                        # 取引実行
                        trade_result = await execute_trade(best_signal, lot_size_usdt)
                        best_signal['trade_result'] = trade_result # 結果をシグナルに格納
                        
                        # 実行結果をログに記録
                        log_signal(best_signal, "取引シグナル")
                        
                        # 成功/失敗通知
                        message = format_telegram_message(best_signal, "取引シグナル", current_threshold, trade_result)
                        await telegram_send_message(message)
                        
                        # クールダウン時間を更新 (成功/失敗に関わらず)
                        LAST_SIGNAL_TIME[best_signal['symbol']] = time.time()
                        
                    else:
                        # スキップ理由を特定
                        if has_position:
                            error_message = f"ポジションを既に保有しています。二重エントリーをスキップします。"
                        else:
                            error_message = f"クールダウン期間中です (次回取引可能: {datetime.fromtimestamp(LAST_SIGNAL_TIME[best_signal['symbol']] + TRADE_SIGNAL_COOLDOWN, JST).strftime('%H:%M:%S')} JST)"
                            
                        trade_result = {'status': 'error', 'error_message': error_message, 'close_status': 'skipped'}
                        logging.warning(f"⚠️ {best_signal['symbol']} 取引スキップ: {error_message}")
                        
                        # スキップ通知 (重要度の低い警告としてログに記録のみ)
                        best_signal['trade_result'] = trade_result
                        log_signal(best_signal, "取引シグナル (スキップ)")
                        
                else:
                    error_message = f"残高不足 (現在: {format_usdt(account_status['total_usdt_balance'])} USDT)。新規取引に必要な額: {MIN_USDT_BALANCE_FOR_TRADE:.2f} USDT。"
                    trade_result = {'status': 'error', 'error_message': error_message, 'close_status': 'skipped'}
                    logging.warning(f"⚠️ {best_signal['symbol']} 取引スキップ: {error_message}")
                    
                    best_signal['trade_result'] = trade_result
                    log_signal(best_signal, "取引シグナル (残高不足)")

            else:
                logging.info(f"ℹ️ {best_signal['symbol']} は閾値 {current_threshold*100:.2f} を満たしません ({best_signal['score']*100:.2f})。取引をスキップします。")
                # 閾値を満たさないシグナルも、ログには残す
                log_signal(best_signal, "分析シグナル (閾値未満)")
                
        else:
            logging.info("ℹ️ 有効な取引シグナルは見つかりませんでした。")
            
        # 7. Hourly Report用のシグナルログを更新
        HOURLY_SIGNAL_LOG.extend([s for s in all_signals if s['score'] >= 0.50])
        
        # 8. 初回起動完了通知 (一度だけ実行)
        if not IS_FIRST_MAIN_LOOP_COMPLETED:
            startup_message = format_startup_message(account_status, GLOBAL_MACRO_CONTEXT, len(CURRENT_MONITOR_SYMBOLS), current_threshold, BOT_VERSION)
            await telegram_send_message(startup_message)
            IS_FIRST_MAIN_LOOP_COMPLETED = True
            logging.info("✅ 初回起動完了通知を送信しました。")
            
        # 9. Hourly Reportの通知 (1時間ごと)
        if time.time() - LAST_HOURLY_NOTIFICATION_TIME >= HOURLY_SCORE_REPORT_INTERVAL:
            # 重複を排除し、最高スコアのシグナルを残す
            latest_signals = {}
            for signal in HOURLY_SIGNAL_LOG:
                key = (signal['symbol'], signal['timeframe'])
                if key not in latest_signals or signal['score'] > latest_signals[key]['score']:
                    latest_signals[key] = signal
                    
            report_message = format_hourly_report(list(latest_signals.values()), HOURLY_ATTEMPT_LOG, LAST_HOURLY_NOTIFICATION_TIME, current_threshold, BOT_VERSION)
            await telegram_send_message(report_message)
            
            # リセット
            HOURLY_SIGNAL_LOG = []
            HOURLY_ATTEMPT_LOG = {}
            LAST_HOURLY_NOTIFICATION_TIME = time.time()
            logging.info("✅ Hourly Reportを送信し、ログをリセットしました。")


    except Exception as e:
        logging.critical(f"🚨 メインBOTループで致命的なエラーが発生: {e}", exc_info=True)
        # 連続的なエラーを防ぐため、強制的にクールダウン
        await asyncio.sleep(LOOP_INTERVAL * 5)
        
    finally:
        end_time = time.time()
        sleep_time = LOOP_INTERVAL - (end_time - start_time)
        if sleep_time > 0:
            # logging.debug(f"ℹ️ メインループ完了。次まで {sleep_time:.2f}秒待機します。")
            await asyncio.sleep(sleep_time)
        else:
            logging.warning(f"⚠️ メインループがオーバーランしました ({-(sleep_time):.2f}秒超過)。即座に再実行します。")
            # オーバーランしても、次のループを待つ

async def monitor_loop_wrapper():
    """オープン注文監視ループのラッパー (並行実行用)"""
    while True:
        try:
            await open_order_management_loop()
        except Exception as e:
            logging.critical(f"🚨 注文監視ループで致命的なエラーが発生: {e}", exc_info=True)
        
        await asyncio.sleep(MONITOR_INTERVAL)

async def main_loop_wrapper():
    """メインBOTループのラッパー (並行実行用)"""
    while True:
        await main_bot_loop()


# ====================================================================================
# FASTAPI / WEB SERVER 
# ====================================================================================

# FastAPIアプリケーションの初期化
app = FastAPI(
    title="Apex BOT API", 
    description="Apex BOT Status and Management Interface", 
    version=BOT_VERSION
)

# バックグラウンドタスク (メインループと監視ループ) の管理
@app.on_event("startup")
async def startup_event():
    """アプリケーション起動時にメインループをバックグラウンドで開始する"""
    logging.info("🚀 FastAPIサーバーが起動しました。BOTループを開始します。")
    # asyncio.create_taskで非同期タスクとして実行
    asyncio.create_task(main_loop_wrapper())
    asyncio.create_task(monitor_loop_wrapper())

# 疎通確認用エンドポイント
@app.get("/health", response_class=JSONResponse)
def health_check():
    """ボットの稼働状態チェック"""
    is_bot_ready = IS_CLIENT_READY and IS_FIRST_MAIN_LOOP_COMPLETED
    status = "OK" if is_bot_ready else "INITIALIZING"
    return JSONResponse(content={"status": status, "version": BOT_VERSION, "client_ready": IS_CLIENT_READY})

# ボットの現在の状態を表示するエンドポイント
@app.get("/status", response_class=JSONResponse)
async def get_bot_status():
    """現在のボットの状態、資産、オープンポジション、最新シグナルを返す"""
    
    current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)

    status_data = {
        "version": BOT_VERSION,
        "is_test_mode": TEST_MODE,
        "is_client_ready": IS_CLIENT_READY,
        "last_success_time_jst": datetime.fromtimestamp(LAST_SUCCESS_TIME, JST).strftime("%Y/%m/%d %H:%M:%S") if LAST_SUCCESS_TIME else "N/A",
        "current_total_equity": GLOBAL_TOTAL_EQUITY,
        "macro_context": GLOBAL_MACRO_CONTEXT,
        "current_signal_threshold": current_threshold,
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
                "sl": format_price_precision(p['stop_loss']),
                "tp": format_price_precision(p['take_profit']),
                "id": p['id'][:8] + '...'
            }
            for p in OPEN_POSITIONS
        ],
    }
    
    # JSONResponseを使用して、意図的にHTMLタグをエンコードせずに返す
    return JSONResponse(content=status_data)

# if __name__ == "__main__":
#     # このブロックはUvicornが直接呼び出すのではなく、uvicorn main_render\ (43):app で実行される
#     # 開発環境でのみ、直接実行したい場合はコメントアウトを解除
#     # uvicorn.run("main_render (43):app", host="0.0.0.0", port=8000, reload=True)
