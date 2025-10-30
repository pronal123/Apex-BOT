# ====================================================================================
# Apex BOT v19.0.32 - High Dispersion Scoring & Dynamic Lot Sizing (Precision Patch)
#
# 改良・修正点:
# 1. 【価格表示】価格情報（Entry, SL, TP）を小数第4位まで表示するように修正。
# 2. 【ロジック維持】v19.0.31で導入された高分散スコアリングロジックは維持。
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
    format='%(asctime)s - %(levelname)s - (%(funcName)s) - %(message)s' 
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
ANALYSIS_ONLY_INTERVAL = 60 * 60    # 分析専用通知の実行間隔 (秒) - 1時間ごと
WEBSHARE_UPLOAD_INTERVAL = 60 * 60  # WebShareログアップロード間隔 (1時間ごと)
MONITOR_INTERVAL = 10               # ポジション監視ループの実行間隔 (秒) - 10秒ごと

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
DYNAMIC_LOT_MAX_PERCENT = 0.50 # 最大ロット (総資産の 50%)
DYNAMIC_LOT_SCORE_MAX = 0.90   # このスコアで最大ロットが適用される (90点)


# 💡 WEBSHARE設定 (HTTP POSTへ変更)
WEBSHARE_METHOD = os.getenv("WEBSHARE_METHOD", "HTTP") # デフォルトはHTTPに変更
WEBSHARE_POST_URL = os.getenv("WEBSHARE_POST_URL", "http://your-webshare-endpoint.com/upload") # HTTP POST用のエンドポイント

# グローバル変数 (状態管理用)
EXCHANGE_CLIENT: Optional[ccxt_async.Exchange] = None
CURRENT_MONITOR_SYMBOLS: List[str] = DEFAULT_SYMBOLS.copy()
LAST_SUCCESS_TIME: float = 0.0
LAST_SIGNAL_TIME: Dict[str, float] = {}
LAST_ANALYSIS_SIGNALS: List[Dict] = []
LAST_HOURLY_NOTIFICATION_TIME: float = 0.0
LAST_ANALYSIS_ONLY_NOTIFICATION_TIME: float = 0.0
LAST_WEBSHARE_UPLOAD_TIME: float = 0.0 
GLOBAL_MACRO_CONTEXT: Dict = {'fgi_proxy': 0.0, 'fgi_raw_value': 'N/A', 'forex_bonus': 0.0} # ★初期値を設定
IS_FIRST_MAIN_LOOP_COMPLETED: bool = False # 初回メインループ完了フラグ
OPEN_POSITIONS: List[Dict] = [] # 現在保有中のポジション (SL/TP監視用)
GLOBAL_TOTAL_EQUITY: float = 0.0 # 総資産額を格納するグローバル変数

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
# 【★スコアリング定数変更 V19.0.31: 高分散スコアリング】
# ====================================================================================
TARGET_TIMEFRAMES = ['1m', '5m', '15m', '1h', '4h'] 

# スコアリングウェイト
BASE_SCORE = 0.50                   # ベースとなる取引基準点 (50点に引き上げ)
LONG_TERM_SMA_LENGTH = 200          # 長期トレンドフィルタ用SMA

# ペナルティ（マイナス要因）
LONG_TERM_REVERSAL_PENALTY = 0.30   # 長期トレンド逆行時のペナルティを強化 (0.20 -> 0.30)
MACD_CROSS_PENALTY = 0.25           # MACDが不利なクロス/発散時のペナルティを強化 (0.15 -> 0.25)
VOLATILITY_BB_PENALTY_THRESHOLD = 0.01 # BB幅が1%未満

# ボーナス（プラス要因）
TREND_ALIGNMENT_BONUS = 0.15        # ★新要因: SMA50 > SMA200 時のボーナス
STRUCTURAL_PIVOT_BONUS = 0.10       # 価格構造/ピボット支持時のボーナスを強化 (0.05 -> 0.10)
RSI_MOMENTUM_LOW = 45               # RSIが45以下でロングモメンタム候補
RSI_MOMENTUM_BONUS_MAX = 0.15       # ★新要因: RSIの強さに応じた可変ボーナスの最大値
OBV_MOMENTUM_BONUS = 0.08           # OBVの確証ボーナスを強化 (0.04 -> 0.08)
VOLUME_INCREASE_BONUS = 0.10        # ★新要因: 出来高スパイク時のボーナス
LIQUIDITY_BONUS_MAX = 0.10          # 流動性(板の厚み)による最大ボーナスを強化 (0.06 -> 0.10)
FGI_PROXY_BONUS_MAX = 0.05          # 恐怖・貪欲指数による最大ボーナス/ペナルティ (変更なし)

# 市場環境に応じた動的閾値調整のための定数 (変更なし)
FGI_SLUMP_THRESHOLD = -0.02         
FGI_ACTIVE_THRESHOLD = 0.02         
SIGNAL_THRESHOLD_SLUMP = 0.90       
SIGNAL_THRESHOLD_NORMAL = 0.85      
SIGNAL_THRESHOLD_ACTIVE = 0.75      

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

def get_estimated_win_rate(score: float) -> str:
    """スコアに基づいて推定勝率を返す"""
    if score >= 0.90:
        return "90%+"
    elif score >= 0.85:
        return "85-90%"
    elif score >= 0.80:
        return "80-85%"
    elif score >= 0.75:
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
            # SL/TPの表示に新しい関数を使用
            sl_display = format_price_precision(OPEN_POSITIONS[0]['stop_loss'])
            tp_display = format_price_precision(OPEN_POSITIONS[0]['take_profit'])
            
            balance_section += (
                f"  - **管理中ポジション**: <code>{len(OPEN_POSITIONS)}</code> 銘柄 (投入合計: <code>{format_usdt(total_managed_value)}</code> USDT)\n"
            )
            for i, pos in enumerate(OPEN_POSITIONS[:3]): # Top 3のみ表示
                base_currency = pos['symbol'].replace('/USDT', '')
                balance_section += f"    - Top {i+1}: {base_currency} (SL: {format_price_precision(pos['stop_loss'])} / TP: {format_price_precision(pos['take_profit'])})\n"
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
        f"<pre>※ この通知はメインの分析ループが一度完了したことを示します。約1分ごとに分析が実行されます。</pre>"
    )

    return header + balance_section + macro_section + footer


def format_telegram_message(signal: Dict, context: str, current_threshold: float, trade_result: Optional[Dict] = None, exit_type: Optional[str] = None) -> str:
    """Telegram通知用のメッセージを作成する【★V19.0.32で価格表示を変更】"""
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
            trade_status_line = "✅ **自動売買 成功**: 現物ロング注文を執行しました。"
            
            filled_amount = trade_result.get('filled_amount', 0.0) 
            filled_usdt = trade_result.get('filled_usdt', 0.0)
            
            trade_section = (
                f"💰 **取引実行結果**\n"
                f"  - **注文タイプ**: <code>現物 (Spot) / 成行買い</code>\n"
                f"  - **動的ロット**: {lot_info} (目標)\n" 
                f"  - **約定数量**: <code>{filled_amount:.4f}</code> {symbol.split('/')[0]}\n"
                f"  - **平均約定額**: <code>{format_usdt(filled_usdt)}</code> USDT\n"
            )
            
    elif context == "ポジション決済":
        exit_type_final = trade_result.get('exit_type', exit_type or '不明')
        trade_status_line = f"🔴 **ポジション決済**: {exit_type_final} トリガー"
        
        entry_price = trade_result.get('entry_price', 0.0)
        exit_price = trade_result.get('exit_price', 0.0)
        pnl_usdt = trade_result.get('pnl_usdt', 0.0)
        pnl_rate = trade_result.get('pnl_rate', 0.0)
        filled_amount = trade_result.get('filled_amount', 0.0)
        
        pnl_sign = "✅ 利益確定" if pnl_usdt >= 0 else "❌ 損切り"
        
        trade_section = (
            f"💰 **決済実行結果** - {pnl_sign}\n"
            # 決済価格も高精度表示
            f"  - **エントリー価格**: <code>{format_price_precision(entry_price)}</code>\n"
            f"  - **決済価格**: <code>{format_price_precision(exit_price)}</code>\n"
            f"  - **約定数量**: <code>{filled_amount:.4f}</code> {symbol.split('/')[0]}\n"
            f"  - **損益**: <code>{'+' if pnl_usdt >= 0 else ''}{format_usdt(pnl_usdt)}</code> USDT ({pnl_rate*100:.2f}%)\n"
        )
            
    
    message = (
        f"🚀 **Apex TRADE {context}**\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - **日時**: {now_jst} (JST)\n"
        f"  - **銘柄**: <b>{symbol}</b> ({timeframe})\n"
        f"  - **ステータス**: {trade_status_line}\n" 
        f"  - **総合スコア**: <code>{score * 100:.2f} / 100</code>\n"
        f"  - **取引閾値**: <code>{current_threshold * 100:.2f}</code> 点\n"
        f"  - **推定勝率**: <code>{estimated_wr}</code>\n"
        f"  - **リスクリワード比率 (RRR)**: <code>1:{rr_ratio:.2f}</code>\n"
        # ★ここから価格表示をformat_price_precisionに変更
        f"  - **エントリー**: <code>{format_price_precision(entry_price)}</code>\n"
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
        
    message += (f"<i>Bot Ver: v19.0.32 - High Dispersion Scoring & Dynamic Lot Sizing (Precision Patch)</i>")
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
        
        # 最終ログ時刻を更新
        LAST_WEBSHARE_UPLOAD_TIME = time.time()
        
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


async def adjust_order_amount(symbol: str, usdt_amount: float) -> Tuple[float, float, float]:
    """USDT建ての注文量を取引所の最小数量、桁数に合わせて調整する"""
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        return 0.0, 0.0, usdt_amount

    try:
        ticker = await EXCHANGE_CLIENT.fetch_ticker(symbol)
        last_price = ticker['last']
        
        # 数量を計算
        base_amount = usdt_amount / last_price
        
        market = EXCHANGE_CLIENT.markets.get(symbol)
        if not market:
            logging.warning(f"⚠️ {symbol}の市場情報が見つかりません。数量の丸め処理をスキップします。")
            return base_amount, last_price, usdt_amount

        # 最小取引数量のチェック
        min_amount = market.get('limits', {}).get('amount', {}).get('min', 0.0)
        if base_amount < min_amount:
            logging.warning(f"⚠️ 注文数量 ({base_amount:.4f}) が最小取引数量 ({min_amount}) を下回りました。最小数量に調整します。")
            base_amount = min_amount

        # 数量の桁数に合わせて丸める
        precision = market.get('precision', {}).get('amount', 4) # 精度がない場合はデフォルト4
        base_amount = EXCHANGE_CLIENT.amount_to_precision(symbol, base_amount)
        
        # 最終的なUSDT金額を再計算
        final_usdt_amount = float(base_amount) * last_price
        
        return float(base_amount), last_price, final_usdt_amount

    except Exception as e:
        logging.error(f"❌ 注文数量の調整に失敗 ({symbol}): {e}")
        return 0.0, 0.0, 0.0

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
    except ccxt.NetworkError as e:
        logging.error(f"❌ OHLCV取得失敗 ({symbol} - {timeframe}): ネットワークエラー。{e}")
    except Exception as e:
        # Mexcなどのマイナーな取引所ではシンボルが存在しないエラーも発生する
        if "Symbol not found" in str(e) or "Invalid symbol" in str(e):
             logging.warning(f"⚠️ {symbol} は取引所に存在しないためスキップします。")
             pass 
        else:
             logging.error(f"❌ OHLCV取得失敗 ({symbol} - {timeframe}): 予期せぬエラー。{e}")
             
    return None

async def fetch_fgi_data() -> Dict:
    """外部APIからFGI (Fear & Greed Index) データを取得し、マクロコンテキストを返す"""
    url = "https://api.alternative.me/fng/?limit=1"
    
    try:
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        data = response.json().get('data', [])
        
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
    macd_data = ta.macd(df['close'], fast=12, slow=26, signal=9)
    df[['MACD', 'MACD_H', 'MACD_S']] = macd_data

    # ボリンジャーバンド
    bbands = ta.bbands(df['close'], length=20, std=2)
    df[['BBL', 'BBM', 'BBU', 'BBB', 'BBP']] = bbands
    
    # OBV
    df['OBV'] = ta.obv(df['close'], df['volume'])
    df['OBV_SMA'] = ta.sma(df['OBV'], length=20)
    
    # 出来高平均
    df['Volume_SMA20'] = ta.sma(df['volume'], length=20)
    
    # ピボットポイント (簡易版)
    df['Pivot'] = (df['high'].shift(1) + df['low'].shift(1) + df['close'].shift(1)) / 3
    df['R1'] = 2 * df['Pivot'] - df['low'].shift(1)
    df['S1'] = 2 * df['Pivot'] - df['high'].shift(1)

    return df


def analyze_signals(df: pd.DataFrame, symbol: str, timeframe: str, macro_context: Dict) -> Optional[Dict]:
    """分析ロジックに基づき、取引シグナルを生成する"""
    global GLOBAL_TOTAL_EQUITY, DYNAMIC_LOT_MIN_PERCENT, DYNAMIC_LOT_MAX_PERCENT, DYNAMIC_LOT_SCORE_MAX, SIGNAL_THRESHOLD
    
    if df.empty or df['SMA200'].isnull().all():
        return None
        
    current_price = df['close'].iloc[-1]
    
    # ----------------------------------
    # 1. ロングシグナル判定 (簡易トレンドフィルタ)
    # ----------------------------------
    # SMA200を上回っていること
    if current_price > df['SMA200'].iloc[-1]:
        
        # --- スコアリングの初期化 ---
        score = BASE_SCORE # 0.50
        
        # --- テクニカルデータ計算 ---
        fgi_proxy = macro_context.get('fgi_proxy', 0.0)
        # マクロ環境ボーナス (FGI)
        sentiment_fgi_proxy_bonus = (fgi_proxy / FGI_ACTIVE_THRESHOLD) * FGI_PROXY_BONUS_MAX if abs(fgi_proxy) <= FGI_ACTIVE_THRESHOLD and FGI_ACTIVE_THRESHOLD > 0 else (FGI_PROXY_BONUS_MAX if fgi_proxy > 0 else -FGI_PROXY_BONUS_MAX)
        
        # Long-Term Reversal Penalty (価格がSMA200から大きく乖離しすぎている場合)
        long_term_reversal_penalty_value = 0.0
        if current_price > df['SMA200'].iloc[-1] * 1.05: # 5%以上乖離
            long_term_reversal_penalty_value = LONG_TERM_REVERSAL_PENALTY 
            
        # NEW FACTOR 1: Mid-Term Trend Alignment Bonus (SMA50 > SMA200)
        trend_alignment_bonus_value = 0.0
        if df['SMA50'].iloc[-1] > df['SMA200'].iloc[-1]:
            trend_alignment_bonus_value = TREND_ALIGNMENT_BONUS
            
        # Structural Pivot Bonus (直近のS1, S2がサポートとして機能している場合)
        structural_pivot_bonus = 0.0
        if df['S1'].iloc[-1] < current_price and df['S1'].iloc[-1] > df['low'].iloc[-2]: 
             structural_pivot_bonus = STRUCTURAL_PIVOT_BONUS

        # MACD Penalty (MACD線がシグナル線の下にある)
        macd_penalty_value = 0.0
        if df['MACD'].iloc[-1] < df['MACD_S'].iloc[-1]:
            macd_penalty_value = MACD_CROSS_PENALTY

        # NEW FACTOR 2: RSI Magnitude Bonus (RSI 50-70 の範囲で線形加点)
        rsi = df['RSI'].iloc[-1]
        rsi_momentum_bonus_value = 0.0
        if rsi >= 50 and rsi < 70:
            # 50で0点、70でRSI_MOMENTUM_BONUS_MAX (0.15)
            rsi_momentum_bonus_value = RSI_MOMENTUM_BONUS_MAX * ((rsi - 50.0) / 20.0)
        # RSIが低すぎる場合はペナルティを考慮しない (ベーススコアに含まれていると見なす)

        # OBV Momentum Bonus (OBVがSMAを上抜けている)
        obv_momentum_bonus_value = 0.0
        if df['OBV'].iloc[-1] > df['OBV_SMA'].iloc[-1] and df['OBV'].iloc[-2] <= df['OBV_SMA'].iloc[-2]:
             obv_momentum_bonus_value = OBV_MOMENTUM_BONUS
             
        # NEW FACTOR 3: Volume Spike Bonus
        volume_increase_bonus_value = 0.0
        if 'Volume_SMA20' in df.columns and df['Volume_SMA20'].iloc[-1] > 0 and df['volume'].iloc[-1] > df['Volume_SMA20'].iloc[-1] * 1.5: # 出来高が平均の1.5倍
            volume_increase_bonus_value = VOLUME_INCREASE_BONUS

        # Volatility Penalty (ボリンジャーバンド幅が狭すぎる場合)
        volatility_penalty_value = 0.0
        if df['BBB'].iloc[-1] < VOLATILITY_BB_PENALTY_THRESHOLD * 100: # BB幅が1%未満
            volatility_penalty_value = -0.05 # ペナルティとしてマイナス5点を付与

        # 流動性ボーナス (板情報は省略しMAXボーナスを固定)
        liquidity_bonus_value = LIQUIDITY_BONUS_MAX 

        tech_data = {
            'long_term_reversal_penalty_value': long_term_reversal_penalty_value, 
            'trend_alignment_bonus_value': trend_alignment_bonus_value, 
            'structural_pivot_bonus': structural_pivot_bonus, 
            'macd_penalty_value': macd_penalty_value, 
            'rsi_momentum_bonus_value': rsi_momentum_bonus_value, 
            'rsi_value': rsi, 
            'obv_momentum_bonus_value': obv_momentum_bonus_value, 
            'volume_increase_bonus_value': volume_increase_bonus_value, 
            'liquidity_bonus_value': liquidity_bonus_value, 
            'sentiment_fgi_proxy_bonus': sentiment_fgi_proxy_bonus, 
            'forex_bonus': 0.0,
            'volatility_penalty_value': volatility_penalty_value,
        }
        
        # 総合スコア計算 (ウェイト強化)
        score += (
            tech_data['trend_alignment_bonus_value'] +       
            tech_data['structural_pivot_bonus'] + 
            tech_data['rsi_momentum_bonus_value'] +          
            tech_data['obv_momentum_bonus_value'] + 
            tech_data['volume_increase_bonus_value'] +       
            tech_data['liquidity_bonus_value'] + 
            tech_data['sentiment_fgi_proxy_bonus'] + 
            tech_data['volatility_penalty_value'] - 
            tech_data['long_term_reversal_penalty_value'] -
            tech_data['macd_penalty_value']
        )
        
        
        ##############################################################
        # 2. 動的なSL/TPとRRRの設定ロジック (スコアと構造を考慮)
        ##############################################################
        
        BASE_RISK_PERCENT = 0.015  # 1.5% のリスク
        PIVOT_SUPPORT_BONUS = tech_data.get('structural_pivot_bonus', 0.0) 
        
        # SL価格の決定
        sl_adjustment = (PIVOT_SUPPORT_BONUS / STRUCTURAL_PIVOT_BONUS) * 0.002 if STRUCTURAL_PIVOT_BONUS > 0 else 0.0
        dynamic_risk_percent = max(0.010, BASE_RISK_PERCENT - sl_adjustment) # 最小1.0%リスク
        stop_loss = current_price * (1 - dynamic_risk_percent)
        
        # RRRの決定 (スコアが高いほどRRRを改善)
        BASE_RRR = 1.5  
        MAX_SCORE_FOR_RRR = 0.85 # このスコアでRRRの最大値に達する
        MAX_RRR = 3.0
        
        current_threshold_base = get_current_threshold(macro_context)
        
        if score > current_threshold_base:
            score_ratio = min(1.0, (score - current_threshold_base) / (MAX_SCORE_FOR_RRR - current_threshold_base) if (MAX_SCORE_FOR_RRR - current_threshold_base) > 0 else 1.0)
            dynamic_rr_ratio = BASE_RRR + (MAX_RRR - BASE_RRR) * score_ratio
        else:
            dynamic_rr_ratio = BASE_RRR 
            
        # TP価格の決定
        take_profit = current_price * (1 + dynamic_risk_percent * dynamic_rr_ratio)
        rr_ratio = dynamic_rr_ratio 
        
        ##############################################################
        # 3. 動的ロットサイズの計算 
        ##############################################################
        
        if GLOBAL_TOTAL_EQUITY > 0:
            
            normalized_score = max(0, score - SIGNAL_THRESHOLD)
            score_range = DYNAMIC_LOT_SCORE_MAX - SIGNAL_THRESHOLD
            
            if score_range > 0:
                adjustment_ratio = min(1.0, normalized_score / score_range)
            else:
                adjustment_ratio = 0.5 
            
            dynamic_percent = DYNAMIC_LOT_MIN_PERCENT + (DYNAMIC_LOT_MAX_PERCENT - DYNAMIC_LOT_MIN_PERCENT) * adjustment_ratio
            calculated_lot_size = GLOBAL_TOTAL_EQUITY * dynamic_percent
            lot_size_usdt = max(calculated_lot_size, BASE_TRADE_SIZE_USDT)
            
            # 銘柄 (symbol) を含めてログ出力
            logging.info(f"💰 動的ロット計算 - {symbol}: Score={score:.2f}, Ratio={dynamic_percent*100:.1f}%, Equity={GLOBAL_TOTAL_EQUITY:.2f} -> Lot={lot_size_usdt:.2f} USDT")
        else:
            lot_size_usdt = BASE_TRADE_SIZE_USDT
            logging.warning(f"⚠️ {symbol}: 総資産額が不明のため、基本ロットサイズを使用します。")
        
        ##############################################################

        # 4. 最終チェック
        current_threshold = get_current_threshold(macro_context)
        
        if score > current_threshold and rr_ratio >= 1.0:
             return {
                'symbol': symbol,
                'timeframe': timeframe,
                'action': 'buy', 
                'score': score,
                'rr_ratio': rr_ratio, 
                'entry_price': current_price,
                'stop_loss': stop_loss, 
                'take_profit': take_profit, 
                'lot_size_usdt': lot_size_usdt, 
                'tech_data': tech_data, 
            }
    return None

async def execute_trade(signal: Dict, account_status: Dict) -> Dict:
    """CCXTを利用して現物取引を実行する"""
    global EXCHANGE_CLIENT
    
    symbol = signal['symbol']
    action = signal['action'] # 'buy'
    lot_size_usdt = signal['lot_size_usdt'] # 動的ロットを使用
    
    if TEST_MODE:
        return {
            'status': 'ok',
            'filled_amount': lot_size_usdt / signal['entry_price'],
            'filled_usdt': lot_size_usdt,
            'id': f"TEST-{uuid.uuid4()}",
            'price': signal['entry_price'],
            'message': 'Test mode: No real trade executed.'
        }

    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        return {'status': 'error', 'error_message': 'CCXTクライアントが未準備です。'}

    # 1. 注文数量の調整
    try:
        base_amount, price_at_order, final_usdt_amount = await adjust_order_amount(symbol, lot_size_usdt)
        if base_amount <= 0:
            return {'status': 'error', 'error_message': '注文数量の調整に失敗しました。最小取引額または残高を確認してください。'}
            
        logging.info(f"🚀 {symbol}: {action} {base_amount:.4f} @ {price_at_order:.4f} (USDT: {final_usdt_amount:.2f})")

    except Exception as e:
        return {'status': 'error', 'error_message': f'注文前処理エラー: {e}'}

    # 2. 注文実行 (成行買い)
    try:
        # Mexcでの現物成行買いは 'market'
        order = await EXCHANGE_CLIENT.create_order(
            symbol=symbol,
            type='market',
            side=action,
            amount=base_amount,
            params={}
        )
        
        # 注文結果の確認 (部分約定や失敗の可能性も考慮)
        if order and order['status'] == 'closed':
            filled_amount = order['filled']
            filled_usdt = order['cost']
            
            if filled_amount > 0 and filled_usdt > 0:
                return {
                    'status': 'ok',
                    'filled_amount': filled_amount,
                    'filled_usdt': filled_usdt,
                    'id': order['id'],
                    'price': order['average'],
                    'message': 'Order successfully filled.'
                }
            else:
                 return {'status': 'error', 'error_message': f"注文は通りましたが、約定数量がゼロです (ID: {order['id']})"}

        elif order and order['status'] in ('open', 'partial'):
            # 成行注文では通常あり得ないが、APIからの応答で open や partial になる可能性も考慮し失敗とみなす
            return {'status': 'error', 'error_message': f"注文は通りましたが、即時約定しませんでした (ステータス: {order['status']}, ID: {order['id']})"}
        
        else:
            return {'status': 'error', 'error_message': f"注文API応答が不正です。ログを確認してください。"}


    except ccxt.ExchangeError as e:
        return {'status': 'error', 'error_message': f'取引所エラー: {e}'}
    except Exception as e:
        return {'status': 'error', 'error_message': f'予期せぬ取引実行エラー: {e}'}

def liquidate_position(position: Dict, current_price: float, exit_type: str) -> Dict:
    """ポジションを強制的に決済し、損益を計算する (テスト用ロジック)"""
    
    entry_price = position['entry_price']
    filled_amount = position['filled_amount']
    filled_usdt = position['filled_usdt']
    
    # 損益計算
    pnl_usdt = (current_price - entry_price) * filled_amount
    pnl_rate = pnl_usdt / filled_usdt
    
    return {
        'status': 'closed',
        'symbol': position['symbol'],
        'entry_price': entry_price,
        'exit_price': current_price,
        'stop_loss': position['stop_loss'],
        'take_profit': position['take_profit'],
        'filled_amount': filled_amount,
        'pnl_usdt': pnl_usdt,
        'pnl_rate': pnl_rate,
        'exit_type': exit_type,
        'id': position['id'],
    }


async def position_management_loop_async():
    """オープンポジションのSL/TPを監視する非同期ループ (10秒ごと)"""
    global OPEN_POSITIONS
    
    if not OPEN_POSITIONS:
        return

    positions_to_close: List[Dict] = []
    closed_position_results: List[Dict] = []
    symbols_to_fetch = [p['symbol'] for p in OPEN_POSITIONS]

    if not symbols_to_fetch or not EXCHANGE_CLIENT:
        return
        
    try:
        # 全ての監視銘柄の最新価格を一括で取得 (tickers APIが利用できない場合は個別に取得)
        tickers = await EXCHANGE_CLIENT.fetch_tickers(symbols_to_fetch)
        
        for position in OPEN_POSITIONS:
            symbol = position['symbol']
            
            ticker = tickers.get(symbol)
            if not ticker or 'last' not in ticker:
                logging.warning(f"⚠️ {symbol} の価格情報が監視ループで取得できませんでした。スキップします。")
                continue
            
            current_price = ticker['last']
            sl = position['stop_loss']
            tp = position['take_profit']
            
            exit_type = None
            
            # SLトリガー判定 (ロングポジション)
            if current_price <= sl:
                exit_type = "SL (ストップロス)"
                
            # TPトリガー判定 (ロングポジション)
            elif current_price >= tp:
                exit_type = "TP (テイクプロフィット)"
                
            
            if exit_type:
                positions_to_close.append(position)
                
                # 決済処理 (テストロジック)
                closed_result = liquidate_position(position, current_price, exit_type)
                
                # 決済通知送信
                current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT) # マクロコンテキストは最新のものを利用
                notification_message = format_telegram_message(position, "ポジション決済", current_threshold, trade_result=closed_result)
                await send_telegram_notification(notification_message)
                
                # ログ記録
                log_signal(closed_result, "Position Exit")

                closed_position_results.append(closed_result)

        # 監視リストから決済されたポジションを削除
        new_open_positions = []
        closed_ids = {p['id'] for p in positions_to_close}
        for p in OPEN_POSITIONS:
            if p['id'] not in closed_ids:
                new_open_positions.append(p)
                
        OPEN_POSITIONS = new_open_positions
        
    except Exception as e:
        logging.error(f"❌ ポジション監視中にエラーが発生: {e}")


# ====================================================================================
# MAIN BOT LOGIC
# ====================================================================================

async def main_bot_loop():
    """ボットのメイン実行ループ (1分ごと)"""
    global LAST_SUCCESS_TIME, LAST_SIGNAL_TIME, LAST_ANALYSIS_SIGNALS, CURRENT_MONITOR_SYMBOLS, GLOBAL_MACRO_CONTEXT, LAST_WEBSHARE_UPLOAD_TIME, IS_FIRST_MAIN_LOOP_COMPLETED
    
    start_time = time.time()
    now_jst = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")
    logging.info(f"--- 💡 {now_jst} - BOT LOOP START (M1 Frequency) ---")
    
    # 1. FGIデータを取得し、GLOBAL_MACRO_CONTEXTを更新
    GLOBAL_MACRO_CONTEXT = await fetch_fgi_data() 
    
    # 2. 口座ステータスの取得 (ロットサイズ計算のため、最新の総資産額を取得)
    account_status = await fetch_account_status()
    current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
    
    new_signals: List[Dict] = []
    
    # 3. 監視銘柄リストの更新 (初回起動時、または定期的に)
    if not IS_FIRST_MAIN_LOOP_COMPLETED or (time.time() - LAST_HOURLY_NOTIFICATION_TIME) > 60 * 60:
        # TODO: ここでCCXTのfetch_tickersやfetch_markets_volumeなどを利用して監視銘柄を更新する
        # 現状はDEFAULT_SYMBOLSを維持
        CURRENT_MONITOR_SYMBOLS = DEFAULT_SYMBOLS.copy() 
        logging.info(f"監視銘柄を {len(CURRENT_MONITOR_SYMBOLS)} 銘柄に更新しました。")

    # 4. 全銘柄の分析とシグナル生成
    for symbol in CURRENT_MONITOR_SYMBOLS:
        
        # 既にオープンポジションがある場合はスキップ (同一銘柄の複数ポジションを避ける)
        if any(p['symbol'] == symbol for p in OPEN_POSITIONS):
            continue

        # クールダウンチェック
        if time.time() - LAST_SIGNAL_TIME.get(symbol, 0) < TRADE_SIGNAL_COOLDOWN:
            continue
            
        # 複数のタイムフレームで分析
        for timeframe in TARGET_TIMEFRAMES:
            limit = REQUIRED_OHLCV_LIMITS[timeframe]
            df = await fetch_ohlcv_safe(symbol, timeframe, limit)
            
            if df is None:
                continue
                
            df = calculate_indicators(df)
            
            signal = analyze_signals(df, symbol, timeframe, GLOBAL_MACRO_CONTEXT)
            
            if signal:
                new_signals.append(signal)
                # 最もスコアの高いシグナルを採用するため、ここではクールダウンは設定しない
                break # より短い足でシグナルが出たら、より長い足の分析はスキップ
    
    # 5. シグナルのフィルタリングと実行
    new_signals.sort(key=lambda x: x['score'], reverse=True)
    
    executed_signals_count = 0
    for signal in new_signals[:TOP_SIGNAL_COUNT]:
        
        # 最終チェック (閾値を再度超えているか確認)
        if signal['score'] < current_threshold:
            continue
        
        # 取引実行 (TEST_MODEでなければ実際に注文)
        trade_result = await execute_trade(signal, account_status)
        
        # 取引結果に基づいて通知
        notification_message = format_telegram_message(signal, "取引シグナル", current_threshold, trade_result)
        await send_telegram_notification(notification_message)
        
        # ログ記録 (WebShare用)
        log_signal(signal, "Trade Signal")

        if trade_result and trade_result['status'] == 'ok':
            executed_signals_count += 1
            LAST_SIGNAL_TIME[signal['symbol']] = time.time()
            
            # 成功した場合、オープンポジションリストに追加 (SL/TP監視用)
            OPEN_POSITIONS.append({
                'id': trade_result.get('id', str(uuid.uuid4())), # IDがない場合はUUIDを生成 (TEST MODE対応)
                'symbol': signal['symbol'],
                'entry_price': trade_result.get('price', signal['entry_price']),
                'stop_loss': signal['stop_loss'],
                'take_profit': signal['take_profit'],
                'filled_amount': trade_result['filled_amount'],
                'filled_usdt': trade_result['filled_usdt'],
                'timeframe': signal['timeframe'],
                'timestamp': time.time(),
            })

    LAST_ANALYSIS_SIGNALS = new_signals # ログ等に使用

    # 6. 初回起動通知の送信
    if not IS_FIRST_MAIN_LOOP_COMPLETED:
        startup_message = format_startup_message(
            account_status, 
            GLOBAL_MACRO_CONTEXT, 
            len(CURRENT_MONITOR_SYMBOLS), 
            current_threshold,
            "v19.0.32 - High Dispersion Scoring & Dynamic Lot Sizing (Precision Patch)"
        )
        await send_telegram_notification(startup_message)
        IS_FIRST_MAIN_LOOP_COMPLETED = True

    # 7. WebShareログのアップロード
    if time.time() - LAST_WEBSHARE_UPLOAD_TIME >= WEBSHARE_UPLOAD_INTERVAL:
        await send_webshare_update({
            'timestamp': datetime.now(JST).isoformat(),
            'signals': _to_json_compatible(LAST_ANALYSIS_SIGNALS),
            'positions': _to_json_compatible(OPEN_POSITIONS),
            'equity': GLOBAL_TOTAL_EQUITY,
            'fgi_raw': GLOBAL_MACRO_CONTEXT['fgi_raw_value'],
            'bot_version': "v19.0.32"
        })

    end_time = time.time()
    LAST_SUCCESS_TIME = end_time
    logging.info(f"--- 💡 BOT LOOP END. Positions: {len(OPEN_POSITIONS)}, New Signals: {executed_signals_count} ---")


# ====================================================================================
# FASTAPI & ASYNC EXECUTION
# ====================================================================================

app = FastAPI(title="Apex BOT Trading API", version="v19.0.32")

@app.get("/")
async def root():
    """ルートエンドポイント (ボットの状態確認用)"""
    return JSONResponse(content={
        "status": "Running",
        "client_ready": IS_CLIENT_READY,
        "test_mode": TEST_MODE,
        "current_positions": len(OPEN_POSITIONS),
        "last_loop_success": datetime.fromtimestamp(LAST_SUCCESS_TIME, JST).strftime("%Y/%m/%d %H:%M:%S") if LAST_SUCCESS_TIME else "N/A",
        "total_equity_usdt": f"{GLOBAL_TOTAL_EQUITY:.2f}",
        "bot_version": "v19.0.32 - High Dispersion Scoring & Dynamic Lot Sizing (Precision Patch)"
    })

@app.post("/webhook")
async def webhook_endpoint(request: Dict):
    """外部システムからの通知を受け取る (必要に応じて実装)"""
    logging.info(f"🔔 Webhookを受信しました: {request.get('event')}")
    return {"message": "Webhook received"}

async def main_loop_scheduler():
    """メインBOTループを定期実行するスケジューラ (クライアント準備完了を待機)"""
    # クライアントの初期化が完了するまで待機
    while not IS_CLIENT_READY:
        logging.info("⏳ クライアント準備完了を待機中...")
        await asyncio.sleep(1) 

    # 初期化が完了したらメインループを開始
    while True:
        try:
            await main_bot_loop()
        except Exception as e:
            logging.critical(f"❌ メインループ実行中に致命的なエラー: {e}", exc_info=True)
            await send_telegram_notification(f"🚨 **致命的なエラー**\nメインループでエラーが発生しました: `{e}`")

        # 待機時間を LOOP_INTERVAL (60秒) に基づいて計算
        # 実行にかかった時間を差し引くことで、正確な周期実行を保証
        elapsed_time = time.time() - LAST_SUCCESS_TIME
        wait_time = max(1, LOOP_INTERVAL - elapsed_time)
        logging.info(f"次のメインループまで {wait_time:.1f} 秒待機します。")
        await asyncio.sleep(wait_time)

async def position_monitor_scheduler():
    """TP/SL監視ループを定期実行するスケジューラ (10秒ごと)"""
    while True:
        try:
            await position_management_loop_async()
        except Exception as e:
            logging.critical(f"❌ ポジション監視ループ実行中に致命的なエラー: {e}", exc_info=True)

        await asyncio.sleep(MONITOR_INTERVAL) # MONITOR_INTERVAL (10秒) ごとに実行


@app.on_event("startup")
async def startup_event():
    """アプリケーション起動時に実行 (タスク起動)"""
    # 初期化タスクをバックグラウンドで開始
    asyncio.create_task(initialize_exchange_client())
    # メインループのスケジューラをバックグラウンドで開始 (1分ごと)
    asyncio.create_task(main_loop_scheduler())
    # ポジション監視のスケジューラをバックグラウンドで開始 (10秒ごと)
    asyncio.create_task(position_monitor_scheduler())
    logging.info("(startup_event) - BOTサービスを開始しました。")

# uvicorn.run(app, host="0.0.0.0", port=8000) # uvicorn実行は環境依存のためコメントアウト
