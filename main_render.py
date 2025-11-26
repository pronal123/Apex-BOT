# ====================================================================================
# Apex BOT v19.0.53 (Patched) - FEATURE: Periodic SL/TP Re-Placing for Unmanaged Orders
#
# 改良・修正点:
# 1. 【SL/TP再設定】open_order_management_loop関数内に、SLまたはTPの注文が片方または両方欠けている場合に、
#    残っている注文をキャンセルし、SL/TP注文を再設定するロジックを追加。
# 2. 【IOC失敗診断維持】v19.0.52で追加したIOC失敗時診断ログを維持。
# 3. 【レポート表示修正】Hourly Reportの分析対象数計算ロジックを修正 (v19.0.53-p1)
# 4. 【通知強化】取引シグナル通知に推定損益(USDT)を表示する機能を追加 (v19.0.53-p1)
# 5. 【★新規】OHLCVデータ不足または指標計算エラー発生銘柄を24時間自動除外する機能を追加。
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

# 💡 【追加】エラー/警告が発生した銘柄と除外期限を格納する辞書 (24時間除外機能用)
# Key: シンボル名 (str), Value: 除外期限 (datetimeオブジェクト)
GLOBAL_EXCLUDED_SYMBOLS: Dict[str, datetime] = {}

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
        logging.warning(f"📉 {log_data['context']} - {log_data['symbol']} ({log_data['timeframe']}): PnL={log_data['pnl_percent']:+.2f}%, Status={log_data['trade_result_status']}")
    elif context == "ポジション決済" and log_data['pnl_percent'] is not None and log_data['pnl_percent'] >= 0:
        # プラス決済はINFO
        logging.info(f"📈 {log_data['context']} - {log_data['symbol']} ({log_data['timeframe']}): PnL={log_data['pnl_percent']:+.2f}%, Status={log_data['trade_result_status']}")
    elif context == "取引シグナル" and log_data['trade_result_status'] == 'ok':
        # 取引成功
        logging.info(f"✅ {log_data['context']} - {log_data['symbol']} ({log_data['timeframe']}): Score={log_data['score']*100:.2f}, Status=OK")
    elif context == "取引シグナル" and log_data['trade_result_status'] == 'error':
        # 取引失敗
        logging.error(f"❌ {log_data['context']} - {log_data['symbol']} ({log_data['timeframe']}): Score={log_data['score']*100:.2f}, Status=ERROR, Message={log_data['error_message']}")
    else:
        # その他、通常シグナル
        logging.debug(f"ℹ️ {log_data['context']} - {log_data['symbol']} ({log_data['timeframe']}): Score={log_data['score']*100:.2f}")

    # JSON形式でファイルにも出力 (オプション、必要に応じて実装)
    # try:
    #     with open('trade_log.jsonl', 'a') as f:
    #         f.write(json.dumps(_to_json_compatible(log_data)) + '\n')
    # except Exception as e:
    #     logging.error(f"❌ ログファイル書き込みエラー: {e}")

async def send_telegram_notification(message: str):
    """Telegramにメッセージを送信する"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.warning("⚠️ TelegramのトークンまたはChat IDが設定されていません。通知をスキップします。")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        'chat_id': TELEGRAM_CHAT_ID,
        'text': message,
        'parse_mode': 'HTML'
    }

    try:
        # ネットワークIOのため、requestsをasyncioでラップして使用 (またはaiohttpなどの非同期ライブラリを使用)
        # ここでは簡単なrequestsを使用
        response = requests.post(url, data=payload, timeout=10)
        response.raise_for_status()
        # logging.debug("✅ Telegram通知を送信しました。")
    except requests.exceptions.HTTPError as errh:
        logging.error(f"❌ Telegram HTTP Error: {errh}")
    except requests.exceptions.ConnectionError as errc:
        logging.error(f"❌ Telegram Connection Error: {errc}")
    except requests.exceptions.Timeout as errt:
        logging.error(f"❌ Telegram Timeout Error: {errt}")
    except requests.exceptions.RequestException as err:
        logging.error(f"❌ Telegram Unknown Error: {err}")

async def initialize_exchange_client():
    """CCXTクライアントを初期化する"""
    global EXCHANGE_CLIENT, IS_CLIENT_READY
    
    if IS_CLIENT_READY and EXCHANGE_CLIENT:
        return

    if CCXT_CLIENT_NAME.lower() == 'mexc':
        ExchangeClass = getattr(ccxt_async, 'mexc')
    elif CCXT_CLIENT_NAME.lower() == 'bybit':
        ExchangeClass = getattr(ccxt_async, 'bybit')
    elif CCXT_CLIENT_NAME.lower() == 'binance':
        ExchangeClass = getattr(ccxt_async, 'binance')
    else:
        logging.critical(f"🚨 サポートされていない取引所クライアント: {CCXT_CLIENT_NAME}")
        sys.exit(1)

    try:
        EXCHANGE_CLIENT = ExchangeClass({
            'apiKey': API_KEY,
            'secret': SECRET_KEY,
            'enableRateLimit': True,
            # 現物取引のための設定（取引所固有のパラメータ）
            'options': {
                'defaultType': 'spot', 
            }
        })
        # 接続テスト (load_marketsが成功すればOKとする)
        await EXCHANGE_CLIENT.load_markets()
        IS_CLIENT_READY = True
        logging.info(f"✅ CCXTクライアント {EXCHANGE_CLIENT.name} の初期化に成功しました。")
    except Exception as e:
        logging.critical(f"❌ CCXTクライアントの初期化または市場情報のロードに失敗: {e}", exc_info=True)
        # エラー時は終了せず、再試行を待つ（ただし、メインループの最初に再初期化されないように注意）
        # IS_CLIENT_READY = False のままにしておく
        pass


async def fetch_account_status() -> Dict:
    """口座ステータス（USDT残高、総資産額）を取得する"""
    global EXCHANGE_CLIENT, GLOBAL_TOTAL_EQUITY
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.warning("⚠️ クライアントが未準備のため、口座ステータス取得をスキップします。")
        return {'total_usdt_balance': 0.0, 'total_equity': 0.0, 'open_positions': [], 'error': True}
    
    try:
        # 1. 残高を取得
        balance = await EXCHANGE_CLIENT.fetch_balance()
        
        # 2. USDT残高を計算
        free_usdt = balance.get('free', {}).get('USDT', 0.0)
        used_usdt = balance.get('used', {}).get('USDT', 0.0)
        total_usdt_balance = free_usdt + used_usdt
        
        # 3. 総資産額 (Equity) を計算 (USDT建ての資産+その他資産のUSDT評価額)
        total_equity = total_usdt_balance
        
        # USDT以外の通貨の評価額を加算
        for currency, amount in balance.get('total', {}).items():
            if currency not in ['USDT', 'USD'] and amount is not None and amount > 0.000001:
                try:
                    symbol = f"{currency}/USDT"
                    # シンボルが取引所に存在するか確認し、存在しない場合はハイフンなしの形式も試す
                    if symbol not in EXCHANGE_CLIENT.markets:
                         if f"{currency}USDT" in EXCHANGE_CLIENT.markets:
                              symbol = f"{currency}USDT"
                         else:
                              continue # シンボルが見つからなければスキップ
                              
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
        logging.info("ℹ️ CCXTクライアント未準備または市場更新スキップ設定のため、デフォルト銘柄を使用します。")
        return DEFAULT_SYMBOLS.copy()

    try:
        # 市場情報を取得
        markets = await EXCHANGE_CLIENT.load_markets()
        
        # 先物/レバレッジトークンを除外し、USDT現物市場のみをフィルタリング
        spot_markets = [
            m for m in markets.values() 
            if m.get('spot') and m.get('quote') == 'USDT' and not m.get('swap')
            and not (m.get('id') and (
                '3S' in m['id'].upper() or '3L' in m['id'].upper() or 'UP' in m['id'].upper() or 'DOWN' in m['id'].upper()
            )) # レバレッジトークンの除外
        ]

        # 全てのティッカーを取得し、出来高順にソート
        tickers = await EXCHANGE_CLIENT.fetch_tickers([m['symbol'] for m in spot_markets])
        
        # 出来高 (quoteVolume) があるものだけを抽出し、降順にソート
        sorted_tickers = sorted(
            [t for t in tickers.values() if t and t.get('quoteVolume')],
            key=lambda t: t['quoteVolume'],
            reverse=True
        )

        # TOP_SYMBOL_LIMITまでを抽出し、DEFAULT_SYMBOLSを常に含める
        top_symbols = [t['symbol'] for t in sorted_tickers[:TOP_SYMBOL_LIMIT]]
        
        # デフォルトリストに含まれているが、TOPリストに含まれていないものを追加
        for default_sym in DEFAULT_SYMBOLS:
            if default_sym not in top_symbols:
                top_symbols.append(default_sym)
                
        logging.info(f"✅ 出来高TOP銘柄リストを更新しました。合計 {len(top_symbols)} 銘柄。")
        return top_symbols

    except Exception as e:
        logging.error(f"❌ 出来高TOP銘柄の取得中にエラーが発生: {e}")
        return DEFAULT_SYMBOLS.copy() # 失敗時はデフォルトリストを返す

async def fetch_fgi_data() -> Dict:
    """Fear & Greed Index (FGI) データと為替レートを取得し、マクロコンテキストを返す"""
    fgi_proxy = 0.0
    fgi_raw_value = 'N/A'
    forex_bonus = 0.0
    
    # 1. Fear & Greed Index (FGI) を取得
    try:
        fgi_url = "https://api.alternative.me/fng/?limit=1"
        fgi_response = requests.get(fgi_url, timeout=5)
        fgi_response.raise_for_status()
        fgi_data = fgi_response.json()
        
        if fgi_data and fgi_data.get('data'):
            index_value = int(fgi_data['data'][0]['value']) # 0-100
            index_value_norm = (index_value - 50) / 50 # -1.0 から +1.0 に正規化
            
            # 恐怖 (0-49): マイナス, 貪欲 (51-100): プラス
            fgi_proxy = index_value_norm * FGI_PROXY_BONUS_MAX # -0.05 から +0.05
            fgi_raw_value = f"{index_value} ({fgi_data['data'][0]['value_classification']})"
            logging.info(f"✅ FGIデータ取得成功: {fgi_raw_value}, Proxy={fgi_proxy:.4f}")

    except Exception as e:
        logging.warning(f"❌ FGIデータ取得中にエラーが発生: {e}")
        # 失敗した場合は、fgi_proxy = 0.0 のまま続行
        pass
    
    # 2. USD/JPY 為替レートボーナス (オプション: 為替レートの変動を考慮する場合)
    # 現在の実装では、為替ボーナスは常に 0.0 ですが、拡張のために残しておきます
    try:
        # (為替レート取得ロジックがあればここに実装)
        forex_bonus = 0.0
    except Exception as e:
        # logging.warning(f"❌ 為替レートデータ取得中にエラーが発生: {e}")
        forex_bonus = 0.0
        pass

    return {
        'fgi_proxy': fgi_proxy,
        'fgi_raw_value': fgi_raw_value,
        'forex_bonus': forex_bonus,
    }

# ====================================================================================
# TECHNICAL ANALYSIS & SCORING LOGIC
# ====================================================================================

async def fetch_ohlcv(symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
    """指定された銘柄とタイムフレームのOHLCVデータを取得する"""
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        return pd.DataFrame()

    try:
        # fetch_ohlcvはタイムスタンプ、始値、高値、安値、終値、出来高のリストを返す
        ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(symbol, timeframe, limit=limit)
        
        if not ohlcv:
            # 💡 データが空の場合も警告ログを出す
            logging.warning(f"⚠️ {symbol} ({timeframe}): OHLCVデータが空です。")
            return pd.DataFrame()

        df = pd.DataFrame(ohlcv)
        df.columns = ['timestamp', 'open', 'high', 'low', 'close', 'volume']
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df = df.set_index('timestamp')
        df = df.drop_duplicates(keep='first')

        required_count = REQUIRED_OHLCV_LIMITS.get(timeframe, 500)
        
        # 💡 【修正】OHLCVデータ不足時の24時間除外ロジック
        if len(df) < required_count:
            logging.warning(f"⚠️ {symbol} ({timeframe}): 必要なOHLCVデータ数 ({required_count}) を取得できませんでした。取得数: {len(df)}")

            global GLOBAL_EXCLUDED_SYMBOLS
            symbol_to_exclude = symbol
            expiry_time = datetime.now(timezone.utc) + timedelta(hours=24)
            
            # 既に除外リストにない、または古い期限で登録されている場合にのみ更新
            if symbol_to_exclude not in GLOBAL_EXCLUDED_SYMBOLS or GLOBAL_EXCLUDED_SYMBOLS[symbol_to_exclude] < expiry_time:
                GLOBAL_EXCLUDED_SYMBOLS[symbol_to_exclude] = expiry_time
                logging.warning(f"🚨 OHLCVデータ不足のため、銘柄 {symbol_to_exclude} を {expiry_time.strftime('%Y-%m-%d %H:%M:%S UTC')} まで監視対象から除外します。")

            # データが不十分な場合は空のDataFrameを返し、指標計算に進まないようにする
            return pd.DataFrame() 

        return df

    except Exception as e:
        logging.error(f"❌ OHLCVデータ取得中にエラーが発生: {symbol} ({timeframe}): {e}")
        return pd.DataFrame()


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
    if bb_data is not None and not bb_data.empty:
        try:
             # 💡 新しいキー名で試行
             bb_upper_key = 'BBU_20_2.0_2.0'
             bb_lower_key = 'BBL_20_2.0_2.0'
             if bb_upper_key in bb_data.columns and bb_lower_key in bb_data.columns:
                 df['BBU'] = bb_data[bb_upper_key]
                 df['BBL'] = bb_data[bb_lower_key]
             else:
                  # 💡 古いキー名で試行 (フォールバック)
                  bb_upper_key = 'BBU_20_2.0'
                  bb_lower_key = 'BBL_20_2.0'
                  df['BBU'] = bb_data[bb_upper_key]
                  df['BBL'] = bb_data[bb_lower_key]
             
             # BB幅の計算 (BBW)
             df['BBW'] = (df['BBU'] - df['BBL']) / df['close'] 
        except KeyError as e:
             logging.warning(f"⚠️ BBANDSのキーエラー: {e} - BBANDS指標をNaNとして処理します。")
             df['BBU'] = np.nan
             df['BBL'] = np.nan
             df['BBW'] = np.nan
    else:
        df['BBU'] = np.nan
        df['BBL'] = np.nan
        df['BBW'] = np.nan

    # Average True Range (ATR)
    df['ATR'] = ta.atr(df['high'], df['low'], df['close'], length=14)
    
    # On-Balance Volume (OBV)
    df['OBV'] = ta.obv(df['close'], df['volume'])
    
    return df

def generate_signal(market_ticker: Dict, df_with_indicators: pd.DataFrame, timeframe: str, macro_context: Dict) -> Optional[Dict]:
    """
    テクニカル指標を分析し、現物買い (ロング) のシグナルを生成し、スコアリングする。
    Args:
        market_ticker: 最新のティッカー情報 (現在価格、板情報)
        df_with_indicators: 計算済み指標を含むOHLCV DataFrame
        timeframe: タイムフレーム ('1h', '4h'など)
        macro_context: FGIなどのマクロ環境情報
    
    Returns:
        シグナル辞書 (score, entry_price, sl/tp, tech_dataなどを含む) または None
    """
    
    # 1. データのバリデーション
    # 最後の行にすべての指標が存在することを確認
    if df_with_indicators.empty:
        return None
        
    last_candle = df_with_indicators.iloc[-1].copy()
    
    # 指標がNaNである行を全て除外した後のDataFrameの長さが、長期SMA長未満の場合は分析を中止
    if df_with_indicators.dropna().shape[0] < LONG_TERM_SMA_LENGTH:
        return None # データが少なすぎるため分析不可

    # 必須データの確認
    required_cols = ['SMA_200', 'SMA_50', 'RSI', 'MACDh', 'ATR', 'BBU', 'BBL', 'BBW']
    if last_candle[required_cols].isnull().any():
        # logging.warning(f"⚠️ {market_ticker['symbol']} ({timeframe}): 必要な指標にNaNが含まれています。")
        return None

    # 2. 現在価格と板情報に基づく流動性ボーナス
    current_price = market_ticker.get('last')
    bid_price = market_ticker.get('bid')
    ask_price = market_ticker.get('ask')
    bid_volume = market_ticker.get('bidVolume', 0.0)
    ask_volume = market_ticker.get('askVolume', 0.0)

    # 現在価格とBID/ASKのチェック
    if not current_price or current_price <= 0 or not bid_price or not ask_price:
         # logging.warning(f"⚠️ {market_ticker['symbol']} ({timeframe}): 価格情報が不完全です。")
         return None

    # 流動性ボーナス計算 (板の厚みとスプレッド)
    liquidity_bonus_value = 0.0
    try:
        if bid_volume > 0 and ask_volume > 0:
            # 出来高ベースの流動性
            volume_liquidity_ratio = min(max(bid_volume, ask_volume) / 500000.0, 1.0) # 50万単位で最大に
            # スプレッドペナルティ (スプレッドが広いほどボーナス減)
            spread_ratio = (ask_price - bid_price) / current_price
            spread_penalty_factor = min(max(spread_ratio / 0.0005, 0.0), 1.0) # 0.05% スプレッドで最大ペナルティ
            
            # 出来高が多いほどボーナスが高く、スプレッドが狭いほどボーナスが高い
            liquidity_bonus_value = LIQUIDITY_BONUS_MAX * volume_liquidity_ratio * (1.0 - spread_penalty_factor)

    except Exception:
        liquidity_bonus_value = 0.0 # 計算エラー時は0

    # 3. エントリー価格、SL、TPの決定
    
    # エントリー価格 (現物買いのため、現在価格に近い指値)
    entry_price = bid_price # BID価格で指値を設定 (即時約定を狙う)
    
    # 長期SMAの上にいるか？ (トレンドフィルター)
    is_above_long_term_sma = current_price > last_candle['SMA_200']

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
        # logging.warning(f"⚠️ {market_ticker['symbol']} ({timeframe}): SL/TP計算が無効です (SL={stop_loss:.4f}, TP={take_profit:.4f})")
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
    # 価格が直近の安値/SMA/BB_Lなどの支持線に近いこと
    structural_pivot_bonus = 0.0
    # 価格がBB下限に近い場合 (低ボラティリティでの反転期待)
    if (last_candle['close'] - last_candle['BBL']) / last_candle['ATR'] < 1.0 :
        structural_pivot_bonus = STRUCTURAL_PIVOT_BONUS
        
    total_score += structural_pivot_bonus
    tech_data['structural_pivot_bonus'] = structural_pivot_bonus

    # E. MACD クロス/発散ペナルティ (25点)
    # MACDヒストグラムがマイナス領域で、かつMACDラインがシグナルラインを下回っている (不利なクロス)
    macd_penalty_value = 0.0
    if last_candle['MACDh'] < 0 and last_candle['MACD'] < last_candle['MACDs']:
        # 不利なクロスが発生している場合、ペナルティ
        macd_penalty_value = MACD_CROSS_PENALTY
        
    total_score -= macd_penalty_value
    tech_data['macd_penalty_value'] = macd_penalty_value

    # F. RSIモメンタムボーナス (10点)
    # RSIが低すぎず（売られすぎではない）、上昇傾向にある (モメンタム加速)
    rsi_momentum_bonus_value = 0.0
    rsi_value = last_candle['RSI']
    tech_data['rsi_value'] = rsi_value # RSI値を記録

    if rsi_value < RSI_MOMENTUM_LOW:
        # RSIが45以下で、かつ直近のRSIが上昇している場合
        if last_candle['RSI'] > df_with_indicators.iloc[-2]['RSI']:
            # RSIが低いほど（買われすぎでなく）ボーナスを大きくする
            # RSIが30のときに最大ボーナス (例)
            if rsi_value < 30:
                 rsi_momentum_bonus_value = RSI_MOMENTUM_BONUS_MAX
            else:
                 # 30-45の間で線形補間
                 ratio = (RSI_MOMENTUM_LOW - rsi_value) / (RSI_MOMENTUM_LOW - 30)
                 rsi_momentum_bonus_value = RSI_MOMENTUM_BONUS_MAX * ratio
            
    total_score += rsi_momentum_bonus_value
    tech_data['rsi_momentum_bonus_value'] = rsi_momentum_bonus_value
    
    # G. OBVモメンタム確証ボーナス (5点)
    # OBVが上昇している (買いの確証)
    obv_momentum_bonus_value = 0.0
    if last_candle['OBV'] > df_with_indicators.iloc[-2]['OBV']:
        obv_momentum_bonus_value = OBV_MOMENTUM_BONUS
        
    total_score += obv_momentum_bonus_value
    tech_data['obv_momentum_bonus_value'] = obv_momentum_bonus_value

    # H. 出来高スパイクボーナス (7点)
    # 直近の出来高が過去20期間平均の2倍以上
    volume_increase_bonus_value = 0.0
    volume_avg = df_with_indicators['volume'].rolling(window=20).mean().iloc[-2]
    if last_candle['volume'] > volume_avg * 2.0:
        volume_increase_bonus_value = VOLUME_INCREASE_BONUS
        
    total_score += volume_increase_bonus_value
    tech_data['volume_increase_bonus_value'] = volume_increase_bonus_value
    
    # I. 流動性ボーナス (7点)
    total_score += liquidity_bonus_value
    tech_data['liquidity_bonus_value'] = liquidity_bonus_value

    # J. ボラティリティペナルティ (低ボラティリティ)
    volatility_penalty_value = 0.0
    if last_candle['BBW'] < VOLATILITY_BB_PENALTY_THRESHOLD:
        # BB幅が狭すぎる (1%未満) 場合、ペナルティ
        volatility_penalty_value = -0.10 # 大きめのペナルティ
    
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

async def analyze_symbol(symbol: str, timeframe: str, market_tickers: Dict, macro_context: Dict) -> Optional[Dict]:
    """単一の銘柄・タイムフレームでOHLCVを取得、分析、シグナルを生成する"""
    
    if symbol not in market_tickers:
        return None # ティッカー情報がない場合はスキップ

    try:
        limit = REQUIRED_OHLCV_LIMITS.get(timeframe, 500)
        df = await fetch_ohlcv(symbol, timeframe, limit)
        
        if df.empty:
            return None # データ取得失敗またはデータ不足の場合はスキップ

        # 指標の計算
        df_with_indicators = calculate_indicators(df.copy())
        
        # シグナルの生成とスコアリング
        signal = generate_signal(market_tickers[symbol], df_with_indicators, timeframe, macro_context)
        
        return signal

    except Exception as e:
        # このエラーは analyze_symbol でキャッチされる
        # main_bot_loopで、このエラーを検出して24時間除外する
        raise Exception(f"OHLCV Fetch/Indicator Calc Error for {symbol} ({timeframe}): {e}")

# ====================================================================================
# ORDER EXECUTION LOGIC
# ====================================================================================

async def adjust_order_amount(symbol: str, target_usdt_amount: float, price: float) -> Tuple[float, float]:
    """
    取引所の最小数量、最小取引額、価格精度に基づいて、注文数量を調整する。
    Returns: (調整後のベース通貨数量, 調整後のUSDT換算額)
    """
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT or symbol not in EXCHANGE_CLIENT.markets:
        logging.error(f"❌ 注文数量調整失敗: 市場情報がありません {symbol}")
        return 0.0, 0.0

    market = EXCHANGE_CLIENT.markets[symbol]
    
    # 1. ロットサイズからベース通貨数量を計算
    base_amount = target_usdt_amount / price
    
    # 2. 数量の丸め精度を取得
    amount_precision = market.get('precision', {}).get('amount', 4) # デフォルトは小数第4位
    
    # 3. 数量を丸める
    # math.floorで切り捨て
    base_amount_rounded = math.floor(base_amount * (10 ** amount_precision)) / (10 ** amount_precision)
    
    # 4. 最小数量 (minAmount) のチェック
    min_amount = market.get('limits', {}).get('amount', {}).get('min', 0.0)
    if base_amount_rounded < min_amount:
        logging.warning(f"⚠️ 数量 {base_amount_rounded:.8f} は最小数量 ({min_amount:.8f}) を満たしません。スキップします。")
        return 0.0, 0.0

    # 5. 最小取引額 (minCost) のチェック (USDT換算)
    min_cost = market.get('limits', {}).get('cost', {}).get('min', 10.0) # デフォルト10 USDT
    final_usdt_amount = base_amount_rounded * price
    if final_usdt_amount < min_cost:
        logging.warning(f"⚠️ 取引額 {final_usdt_amount:.2f} USDT は最小取引額 ({min_cost:.2f} USDT) を満たしません。スキップします。")
        return 0.0, 0.0
        
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
             # ccxtでstop_lossがサポートされていない場合、create_orderでstop_limitを使用
             
             # トリガー価格: SL価格 (Stop Loss Trigger Price)
             trigger_price = stop_loss
             # リミット価格: 実行価格 (Limit Price) - SL価格よりわずかに低い価格 (例: 0.1%低い)
             limit_price = stop_loss * 0.999 

             # 注文タイプ 'stop_limit' または取引所固有のパラメータを使用
             if 'stop_limit' in EXCHANGE_CLIENT.options.get('spot', {}).get('types', []):
                 order_type = 'stop_limit'
                 params = {
                     'stopPrice': trigger_price, # トリガー価格
                     'clientOrderId': f'SL-{uuid.uuid4()}'
                 }
             else:
                 # 取引所固有のパラメータでStop Limitをシミュレート
                 order_type = 'limit' # 実際はlimitではなく、取引所固有のストップタイプを使用
                 
                 # 例: MEXCのStop Limit (paramsでstopPriceとStopLimitTypeを指定)
                 params = {
                     'stopPrice': trigger_price,
                     'stopLimitType': 'StopLoss', # または 'StopLossLimit'
                     'clientOrderId': f'SL-{uuid.uuid4()}'
                 }
                 # CCXTのバージョンや取引所によって異なるため、この部分はカスタマイズが必要
                 # ここでは汎用的な create_order + params で試行
            
             # SL価格でストップリミット売り
             sl_order = await EXCHANGE_CLIENT.create_order(
                 symbol=symbol,
                 type=order_type, # 'limit' または 'stop_limit'
                 side='sell',
                 amount=filled_amount,
                 price=limit_price, # リミット価格
                 params=params
             )
             sl_order_id = sl_order['id']
             logging.info(f"✅ SL注文成功: ID={sl_order_id}, StopPrice={format_price_precision(trigger_price)}")
        else:
             logging.error("❌ SL注文失敗: 取引所がストップリミット注文をサポートしていません。")
             return {'status': 'error', 'error_message': 'SL注文失敗: ストップリミット注文がサポートされていません。'}
        
    except Exception as e:
        # SL注文失敗は致命的。TP注文をキャンセルする必要がある
        logging.error(f"❌ SL注文失敗 ({symbol}): {e}")
        try:
             # 既に設定されているTP注文をキャンセル
             if tp_order_id:
                  await EXCHANGE_CLIENT.cancel_order(tp_order_id, symbol)
                  logging.warning(f"⚠️ SL失敗のため、TP注文 {tp_order_id} をキャンセルしました。")
        except Exception as cancel_e:
             logging.error(f"❌ TP注文のキャンセル失敗 ({symbol}): {cancel_e}")
             
        return {'status': 'error', 'error_message': f'SL注文失敗: {e}'}
        
    return {'status': 'ok', 'sl_order_id': sl_order_id, 'tp_order_id': tp_order_id}


async def close_position_immediately(symbol: str, amount: float) -> Dict:
    """
    ポジションを成行売りで即時クローズする。
    これは主に、エントリー後のSL/TP設定に失敗した場合の緊急措置として使用される。
    Returns: {'status': 'ok', 'closed_amount': float} または {'status': 'error', 'error_message': str}
    """
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY or amount <= 0:
        return {'status': 'skipped', 'error_message': 'スキップ: クライアント未準備または数量がゼロです。'}

    logging.warning(f"🚨 不完全ポジションの強制クローズを試みます: {symbol} (数量: {amount:.4f})")
    
    try:
        # 数量の丸め（成行注文でも精度は重要）
        # 概算でロットサイズ計算 (最新の価格をCCXTから取得する必要があるが、ここでは簡略化のため適当な価格で調整を試みる)
        # より安全に、現物の残高をfetchして全量売却するロジックが望ましい。
        # ここでは、元の注文数量 (amount) を取引所精度で丸めるのみに留める
        
        # 概算価格を取得できれば最良
        try:
            ticker = await EXCHANGE_CLIENT.fetch_ticker(symbol)
            current_price = ticker['last']
        except:
            current_price = 100.0 # フォールバック価格

        # 数量調整関数を使い、target_usdt_amountをamount * current_price * 1.01として呼び出し、
        # 調整された base_amount_rounded を取得
        base_amount_rounded, _ = await adjust_order_amount(
            symbol, 
            amount * current_price * 1.01, # 調整関数に渡すのはUSDT換算額。ここではamountが数量なので、価格をかけてUSDTに。少し多めに設定。
            current_price
        )
        
        # adjust_order_amountの内部ロジックによっては、価格を計算に必要な引数として使用し、amountを直接丸める処理がない可能性があるため、
        # ここでは単純にamountを市場精度で丸める ccxt.decimal_to_precision を使用するのが最も安全。
        amount_precision = EXCHANGE_CLIENT.markets[symbol]['precision']['amount']
        amount_to_sell = EXCHANGE_CLIENT.decimal_to_precision(amount, ccxt.ROUND_DOWN, amount_precision)
        amount_to_sell = float(amount_to_sell)
        
        if amount_to_sell <= 0:
             logging.error(f"❌ 強制クローズ失敗: 調整後の数量がゼロです。")
             return {'status': 'error', 'error_message': '調整後の数量がゼロです。'}
        
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

    logging.info(f"⏳ 現物指値買い注文を発注中: {symbol} @ {format_price_precision(entry_price)}. ロット: {format_usdt(final_usdt_amount)} USDT ({base_amount_to_buy:.4f} 数量)")
    
    # 取引結果の初期値
    trade_result = {
        'status': 'error', 
        'error_message': '初期化エラー', 
        'filled_amount': 0.0, 
        'filled_usdt': 0.0, 
        'sl_order_id': None, 
        'tp_order_id': None,
        'entry_price': entry_price # 注文価格をエントリー価格として記録
    }

    try:
        # 3. 現物指値買い注文 (IOC: Immediate-Or-Cancel)
        # IOC注文: 即時約定しなかった部分はキャンセルされる
        buy_order = await EXCHANGE_CLIENT.create_order(
            symbol=symbol,
            type='limit',
            side='buy',
            amount=base_amount_to_buy,
            price=entry_price,
            params={
                'timeInForce': 'IOC' # 即時約定またはキャンセル
            }
        )
        
        # 4. 約定結果の確認
        filled_amount = buy_order.get('filled', 0.0)
        filled_usdt = buy_order.get('cost', 0.0)
        
        if filled_amount > 0:
            avg_entry_price = buy_order.get('average', entry_price) # 平均約定価格
            
            logging.info(f"✅ 買い注文成功: {symbol} - {filled_amount:.4f} 数量を平均 {format_price_precision(avg_entry_price)} で約定。")
            
            # 5. SL/TP注文の設定
            sl_tp_result = await place_sl_tp_orders(
                symbol=symbol,
                filled_amount=filled_amount,
                stop_loss=signal['stop_loss'],
                take_profit=signal['take_profit']
            )

            if sl_tp_result['status'] == 'ok':
                # SL/TP設定成功
                trade_result = {
                    'status': 'ok',
                    'filled_amount': filled_amount,
                    'filled_usdt': filled_usdt,
                    'entry_price': avg_entry_price,
                    'sl_order_id': sl_tp_result['sl_order_id'],
                    'tp_order_id': sl_tp_result['tp_order_id'],
                    'close_status': 'skipped'
                }
                
                # 6. ポジション管理リストに追加
                position_id = str(uuid.uuid4())
                OPEN_POSITIONS.append({
                    'id': position_id,
                    'symbol': symbol,
                    'entry_price': avg_entry_price,
                    'stop_loss': signal['stop_loss'],
                    'take_profit': signal['take_profit'],
                    'filled_amount': filled_amount,
                    'filled_usdt': filled_usdt,
                    'sl_order_id': sl_tp_result['sl_order_id'],
                    'tp_order_id': sl_tp_result['tp_order_id'],
                    'created_at': time.time()
                })
                
            else:
                # SL/TP設定失敗 -> ポジションを強制クローズ
                logging.error(f"❌ SL/TP設定失敗。ポジションを強制クローズします: {sl_tp_result['error_message']}")
                
                close_result = await close_position_immediately(symbol, filled_amount)

                trade_result = {
                    'status': 'error',
                    'error_message': sl_tp_result['error_message'],
                    'filled_amount': filled_amount,
                    'filled_usdt': filled_usdt,
                    'entry_price': avg_entry_price,
                    'sl_order_id': None,
                    'tp_order_id': None,
                    'close_status': close_result['status'],
                    'closed_amount': close_result.get('closed_amount', 0.0),
                    'close_error_message': close_result.get('error_message'),
                }
            
        else:
            # IOC注文で全く約定しなかった
            error_message = f"指値買い注文が即時約定しなかったためキャンセルされました。"
            logging.warning(f"⚠️ 買い注文スキップ ({symbol}): {error_message}")
            trade_result = {'status': 'error', 'error_message': error_message, 'close_status': 'skipped'}
            
        return trade_result

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
        exit_type = None
        closed_result = None
        
        # 1. SL/TP注文のステータスチェック
        sl_status = await check_order_status(sl_order_id, symbol)
        tp_status = await check_order_status(tp_order_id, symbol)
        
        # SLまたはTPが約定済み/キャンセル済みの場合
        sl_closed = sl_status and sl_status['status'] in ['closed', 'canceled']
        tp_closed = tp_status and tp_status['status'] in ['closed', 'canceled']

        # 2. 決済ロジック
        if sl_closed and sl_status['status'] == 'closed' and sl_status.get('filled') > 0:
            # SLが約定した
            is_closed = True
            exit_type = 'SL約定'
            # 残っているTP注文をキャンセル
            if tp_status and tp_status['status'] not in ['closed', 'canceled']:
                 await EXCHANGE_CLIENT.cancel_order(tp_order_id, symbol)
                 logging.info(f"✅ SL約定に伴い、TP注文 {tp_order_id} をキャンセルしました。")
                 
        elif tp_closed and tp_status['status'] == 'closed' and tp_status.get('filled') > 0:
            # TPが約定した
            is_closed = True
            exit_type = 'TP約定'
            # 残っているSL注文をキャンセル
            if sl_status and sl_status['status'] not in ['closed', 'canceled']:
                 await EXCHANGE_CLIENT.cancel_order(sl_order_id, symbol)
                 logging.info(f"✅ TP約定に伴い、SL注文 {sl_order_id} をキャンセルしました。")

        # 💡 【v19.0.53-p1で追加】3. 不完全な注文の再設定ロジック
        # どちらか片方のみが「closed」または「canceled」で、もう片方が「open」の場合、外部要因でキャンセルされた可能性を考慮し、再設定を試みる。
        sl_open = sl_status and sl_status['status'] == 'open'
        tp_open = tp_status and tp_status['status'] == 'open'
        
        if not is_closed and (sl_closed or tp_closed) and (sl_open or tp_open):
             logging.warning(f"⚠️ {symbol} - SL/TP注文の片方または両方が欠損しています (SL: {sl_status['status'] if sl_status else 'N/A'}, TP: {tp_status['status'] if tp_status else 'N/A'})。再設定を試みます。")
             
             # 現在の注文を全てキャンセル
             if sl_open:
                 await EXCHANGE_CLIENT.cancel_order(sl_order_id, symbol)
             if tp_open:
                 await EXCHANGE_CLIENT.cancel_order(tp_order_id, symbol)
                 
             # ポジションを再取得（現物残高から） - 簡略化のため、元のfilled_amountを使用
             re_place_result = await place_sl_tp_orders(
                 symbol=symbol,
                 filled_amount=position['filled_amount'],
                 stop_loss=position['stop_loss'],
                 take_profit=position['take_profit']
             )
             
             if re_place_result['status'] == 'ok':
                 # 成功した場合、position情報を更新
                 position['sl_order_id'] = re_place_result['sl_order_id']
                 position['tp_order_id'] = re_place_result['tp_order_id']
                 position['created_at'] = time.time() # タイムスタンプ更新
                 logging.info(f"✅ {symbol} のSL/TP注文を再設定しました。")
             else:
                 # 再設定失敗は、ポジションが外部で決済された、または致命的なエラーを示唆
                 logging.error(f"❌ {symbol} のSL/TP注文の再設定に失敗しました: {re_place_result['error_message']}")
                 # ポジションの削除は行わない (手動決済を促す)
             
             pass # このポジションのループは一旦終了し、次回再チェック
        
        elif not is_closed and sl_open and tp_open:
             # 両方オープン中の場合は何もしない
             # logging.debug(f"ℹ️ {symbol} は引き続きオープン中 (SL: {sl_open}, TP: {tp_open})")
             pass 
             
        # 4. ポジション決済が確認された場合の処理
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
                signal_for_log = {'symbol': symbol, 'timeframe': '1h', 'score': SIGNAL_THRESHOLD_NORMAL}
                notification_message = format_telegram_message(signal_for_log, "ポジション決済", SIGNAL_THRESHOLD_NORMAL, closed_result)
                await send_telegram_notification(notification_message)
                log_signal(closed_result, "ポジション決済")

            except Exception as e:
                logging.error(f"❌ 決済処理後のPnL計算/通知中にエラーが発生 ({symbol}): {e}")
                
    # 決済完了したポジションをリストから削除
    OPEN_POSITIONS = [p for p in OPEN_POSITIONS if p['id'] not in positions_to_remove_ids]
    
    logging.debug(f"ℹ️ オープン注文監視ループ終了。残りのポジション: {len(OPEN_POSITIONS)}")


async def run_open_order_management():
    """オープン注文監視ループを定期的に実行する"""
    while True:
        try:
            await open_order_management_loop()
        except Exception as e:
            logging.error(f"❌ オープン注文監視ループで致命的なエラーが発生: {e}", exc_info=True)
            
        await asyncio.sleep(MONITOR_INTERVAL) # 10秒ごとに実行


# ====================================================================================
# MAIN BOT LOGIC
# ====================================================================================

async def main_bot_loop():
    """ボットのメイン実行ループ (1分ごと)"""
    global LAST_SUCCESS_TIME, LAST_SIGNAL_TIME, LAST_ANALYSIS_SIGNALS, CURRENT_MONITOR_SYMBOLS, GLOBAL_MACRO_CONTEXT, LAST_HOURLY_NOTIFICATION_TIME, IS_FIRST_MAIN_LOOP_COMPLETED, HOURLY_SIGNAL_LOG, HOURLY_ATTEMPT_LOG, BOT_VERSION
    
    start_time = time.time()
    now_jst = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")
    logging.info(f"--- 💡 {now_jst} - BOT LOOP START (M1 Frequency) ---")

    try:
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

        # 💡 【追加/修正】GLOBAL_EXCLUDED_SYMBOLSをチェックし、除外期限が切れた銘柄を削除・復帰させる
        now = datetime.now(timezone.utc)
        symbols_to_exclude = []
        symbols_to_reactivate = []
        global GLOBAL_EXCLUDED_SYMBOLS

        # 期限切れチェックとリスト作成
        for symbol, expiry_time in list(GLOBAL_EXCLUDED_SYMBOLS.items()):
            if now < expiry_time:
                symbols_to_exclude.append(symbol)
            else:
                symbols_to_reactivate.append(symbol)
                del GLOBAL_EXCLUDED_SYMBOLS[symbol] # 期限切れのため削除
                
        if symbols_to_reactivate:
            logging.info(f"✅ 以下の銘柄を除外期限切れのため監視に復帰させました: {', '.join(symbols_to_reactivate)}")


        # 5. 全ての監視銘柄の分析タスクを作成
        task_args = []
        
        # 全シンボルと全タイムフレームの組み合わせを作成
        all_symbol_timeframe_tuples = [(symbol, tf) for symbol in CURRENT_MONITOR_SYMBOLS for tf in TARGET_TIMEFRAMES]
        
        # 監視対象リストから現在除外対象の銘柄を除外した、新しいリストを作成
        symbols_to_analyze = [
            symbol_tf for symbol_tf in all_symbol_timeframe_tuples 
            if symbol_tf[0] not in symbols_to_exclude
        ]

        logging.info(f"⏳ {len(symbols_to_analyze)} 個のOHLCVデータ取得と分析タスクを実行中 (除外中: {len(symbols_to_exclude)} 銘柄).")


        # 💡 【修正】タスク生成ループは symbols_to_analyze を使用
        for symbol, timeframe in symbols_to_analyze:
            # 既に保有中の銘柄は分析をスキップし、hourly_attempt_logに記録
            if symbol in [p['symbol'] for p in OPEN_POSITIONS]:
                HOURLY_ATTEMPT_LOG[symbol] = "保有中"
                continue
                
            # ティッカー情報がない銘柄は分析をスキップし、hourly_attempt_logに記録
            if symbol not in market_tickers:
                 HOURLY_ATTEMPT_LOG[symbol] = "ティッカー無し"
                 continue
                 
            # 出来高が少ない銘柄はスキップ (最低100万USDT/24h)
            quoteVolume = market_tickers[symbol].get('quoteVolume', 0.0)
            if quoteVolume < 1000000:
                HOURLY_ATTEMPT_LOG[symbol] = "低出来高"
                continue

            # 直近のシグナルクールダウンをチェック (2時間以内はスキップ)
            last_signal_time = LAST_SIGNAL_TIME.get(symbol, 0.0)
            if time.time() - last_signal_time < TRADE_SIGNAL_COOLDOWN:
                 HOURLY_ATTEMPT_LOG[symbol] = "クールダウン中"
                 continue

            task_args.append(analyze_symbol(symbol, timeframe, market_tickers, GLOBAL_MACRO_CONTEXT))

        # 6. 分析タスクを並行実行
        if task_args:
            results = await asyncio.gather(*task_args, return_exceptions=True)
        else:
            results = []

        # 7. 結果を処理
        valid_signals: List[Dict] = []
        
        for result in results:
            if isinstance(result, Exception):
                # analyze_symbol内で発生したOHLCV取得/指標計算エラーをログに出力
                # logging.error(f"❌ 並列タスク実行中にエラーが発生: {result}")
                continue # エラーはメインのexceptで処理されるため、ここではスキップ
            
            if result and result['score'] >= BASE_SCORE:
                # ベーススコア (0.50) 以上のシグナルのみを有効シグナルとして採用
                valid_signals.append(result)
                
                # Hourly Report用にログに追加 (重複を避けるため、既存のものを置き換え)
                found = False
                for i, log in enumerate(HOURLY_SIGNAL_LOG):
                    if log['symbol'] == result['symbol'] and log['timeframe'] == result['timeframe']:
                        HOURLY_SIGNAL_LOG[i] = result
                        found = True
                        break
                if not found:
                    HOURLY_SIGNAL_LOG.append(result)

        LAST_ANALYSIS_SIGNALS = valid_signals
        
        # 8. 初回起動完了通知 (一度だけ)
        if not IS_FIRST_MAIN_LOOP_COMPLETED:
            startup_message = format_startup_message(account_status, GLOBAL_MACRO_CONTEXT, len(CURRENT_MONITOR_SYMBOLS), current_threshold, BOT_VERSION)
            await send_telegram_notification(startup_message)
            IS_FIRST_MAIN_LOOP_COMPLETED = True

        # 9. ベストシグナルを抽出 (最高スコア)
        best_signal: Optional[Dict] = None
        if valid_signals:
            # スコアとRSIモメンタムボーナスでソート
            best_signal = max(valid_signals, key=lambda x: (x['score'], x['tech_data'].get('rsi_momentum_bonus_value', 0.0)))
            log_signal(best_signal, "取引シグナル (候補)")

        # 10. 取引実行ロジック
        trade_result: Optional[Dict] = None
        
        if best_signal:
            # 閾値チェック
            if best_signal['score'] >= current_threshold and not TEST_MODE:
                 # 残高チェック
                 if account_status.get('total_usdt_balance', 0.0) >= MIN_USDT_BALANCE_FOR_TRADE:
                      # 取引実行
                      trade_result = await execute_trade(best_signal, account_status)
                      
                      if trade_result['status'] == 'ok':
                           LAST_SIGNAL_TIME[best_signal['symbol']] = time.time()
                           log_signal(trade_result, "取引シグナル (成功)")
                      else:
                           log_signal(trade_result, "取引シグナル (失敗)")
                           
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
        elif trade_result and trade_result.get('status') == 'error':
            # 取引失敗
            notification_message = format_telegram_message(best_signal, "取引シグナル", current_threshold, trade_result)
            await send_telegram_notification(notification_message)

        # 12. 1時間ごとのレポート通知
        if time.time() - LAST_HOURLY_NOTIFICATION_TIME >= HOURLY_SCORE_REPORT_INTERVAL:
            report_message = format_hourly_report(
                signals=HOURLY_SIGNAL_LOG, 
                attempt_log=HOURLY_ATTEMPT_LOG,
                start_time=LAST_HOURLY_NOTIFICATION_TIME if LAST_HOURLY_NOTIFICATION_TIME > 0 else start_time,
                current_threshold=current_threshold,
                bot_version=BOT_VERSION
            )
            await send_telegram_notification(report_message)
            LAST_HOURLY_NOTIFICATION_TIME = time.time()
            HOURLY_SIGNAL_LOG = [] # ログをリセット
            HOURLY_ATTEMPT_LOG = {} # ログをリセット

        # 13. エラー処理
    except Exception as e:
        error_message = str(e)
        logging.error(f"❌ 並列分析中にエラーが発生: {error_message}", exc_info=True)
        
        # 💡 【追加】OHLCV/指標計算エラーの場合、該当銘柄を24時間除外するロジック
        if "OHLCV Fetch/Indicator Calc Error for" in error_message:
            # 例: OHLCV Fetch/Indicator Calc Error for USD1/USDT (1m): 'BBM_20_2.0' から銘柄名を抽出
            match = re.search(r"OHLCV Fetch/Indicator Calc Error for (.*?)\s*\(", error_message)
            if match:
                symbol_to_exclude = match.group(1).strip()
                # 現在時刻から24時間後を除外期限とする
                expiry_time = datetime.now(timezone.utc) + timedelta(hours=24) 
                
                global GLOBAL_EXCLUDED_SYMBOLS
                # 既に除外リストにないか確認し、追加
                if symbol_to_exclude not in GLOBAL_EXCLUDED_SYMBOLS:
                    GLOBAL_EXCLUDED_SYMBOLS[symbol_to_exclude] = expiry_time
                    logging.warning(f"🚨 致命的なエラー発生のため、銘柄 {symbol_to_exclude} を {expiry_time.strftime('%Y-%m-%d %H:%M:%S UTC')} まで監視対象から除外します。")
            
        # 💡 致命的エラー発生をTelegramに通知 (v19.0.35で追加)
        telegram_error_message = (
            f"🚨 **致命的なエラー発生**\n\n"
            f"メインBOTループの実行中にエラーが発生しました。\n\n"
            f"**エラー:** <code>{str(e)[:500]}...</code>\n\n"
            f"**BOTバージョン**: <code>{BOT_VERSION}</code>"
        )
        try:
             await send_telegram_notification(telegram_error_message)
             logging.info(f"✅ 致命的エラー通知を送信しました (Ver: {BOT_VERSION})")
        except Exception as notify_e:
             logging.error(f"❌ 致命的エラー通知の送信に失敗: {notify_e}")

    # 次のループまで待機
    await asyncio.sleep(LOOP_INTERVAL)


# ====================================================================================
# APP SETUP & MAIN EXECUTION
# ====================================================================================

app = FastAPI()

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
        "excluded_symbols_count": len(GLOBAL_EXCLUDED_SYMBOLS), # 💡 追加
        "excluded_symbols": {s: t.strftime('%Y-%m-%d %H:%M:%S UTC') for s, t in GLOBAL_EXCLUDED_SYMBOLS.items()}, # 💡 追加
        "last_analysis_signals_count": len(LAST_ANALYSIS_SIGNALS),
    }
    
    return JSONResponse(content=status_data)

@app.on_event("startup")
async def startup_event():
    """アプリケーション起動時に実行されるタスク"""
    # 1. CCXTクライアントの初期化
    await initialize_exchange_client()
    
    # 2. オープン注文監視ループをバックグラウンドで開始
    asyncio.create_task(run_open_order_management())
    
    # 3. メインのボットループをバックグラウンドで開始
    asyncio.create_task(main_bot_loop())
    
if __name__ == "__main__":
    # uvicornでFastAPIアプリケーションを起動
    # 開発環境では reload=True を設定
    uvicorn.run(app, host="0.0.0.0", port=8000)
