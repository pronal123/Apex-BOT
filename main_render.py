# ====================================================================================
# Apex BOT v20.0.25 - Future Trading / 10x Leverage 
# (Patch 71: MEXC Min Notional Value FIX for Lot Size 400)
#
# 改良・修正点:
# 1. 【ロットサイズ修正: Patch 71】execute_trade_logic にて、最小取引単位 (Min Amount) のチェックに加え、
#    最小名目価値 (Min Notional Value / Code 400の原因) をチェックし、満たさない場合は注文をスキップ。
# 2. 【エラー処理維持】Code 10007 (symbol not support api) および Code 30005 (流動性不足) の検出・スキップロジックを維持。
# 3. 【NaN/NoneTypeエラー修正】get_historical_ohlcv 関数に df.dropna() を追加し、データ分析の安定性を向上させました。
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
import math # 数値計算ライブラリ

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
    "FLOW/USDT", "IMX/USDT", 
]
TOP_SYMBOL_LIMIT = 40               # 監視対象銘柄の最大数 (出来高TOPから選出)
LOOP_INTERVAL = 60 * 1              # メインループの実行間隔 (秒) - 1分ごと
ANALYSIS_ONLY_INTERVAL = 60 * 60    # 分析専用通知の実行間隔 (秒) - 1時間ごと
WEBSHARE_UPLOAD_INTERVAL = 60 * 60  # WebShareログアップロード間隔 (1時間ごと)
MONITOR_INTERVAL = 10               # ポジション監視ループの実行間間隔 (秒) - 10秒ごと

# 💡 クライアント設定
CCXT_CLIENT_NAME = os.getenv("EXCHANGE_CLIENT", "mexc")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
API_KEY = os.getenv(f"{CCXT_CLIENT_NAME.upper()}_API_KEY")
SECRET_KEY = os.getenv(f"{CCXT_CLIENT_NAME.upper()}_SECRET")
TEST_MODE = os.getenv("TEST_MODE", "False").lower() in ('true', '1', 't')
SKIP_MARKET_UPDATE = os.getenv("SKIP_MARKET_UPDATE", "False").lower() in ('true', '1', 't')

# 💡 先物取引設定 
LEVERAGE = 10 # 取引倍率
TRADE_TYPE = 'future' # 取引タイプ
MIN_MAINTENANCE_MARGIN_RATE = 0.005 # 最低維持証拠金率 (例: 0.5%) - 清算価格計算に使用

# 💡 レートリミット対策用定数を追加 (修正点: 0.5秒 -> 1.5秒に増加)
LEVERAGE_SETTING_DELAY = 2.0 # レバレッジ設定時のAPIレートリミット対策用遅延 (秒) - 0.5秒から1.5秒に増加

# 💡 リスクベースの動的ポジションサイジング設定 
# BASE_TRADE_SIZE_USDTはリスクベースサイジングにより無視されますが、互換性のために残します。
try:
    BASE_TRADE_SIZE_USDT = float(os.getenv("BASE_TRADE_SIZE_USDT", "100")) 
except ValueError:
    BASE_TRADE_SIZE_USDT = 100.0
    
MAX_RISK_PER_TRADE_PERCENT = float(os.getenv("MAX_RISK_PER_TRADE_PERCENT", "0.01")) # 最大リスク: 総資産の1%

# 💡 WEBSHARE設定 
WEBSHARE_METHOD = os.getenv("WEBSHARE_METHOD", "HTTP") 
WEBSHARE_POST_URL = os.getenv("WEBSHARE_POST_URL", "http://your-webshare-endpoint.com/upload") 

# グローバル変数 (状態管理用)
EXCHANGE_CLIENT: Optional[ccxt_async.Exchange] = None
CURRENT_MONITOR_SYMBOLS: List[str] = DEFAULT_SYMBOLS.copy()
LAST_SUCCESS_TIME: float = 0.0
LAST_SIGNAL_TIME: Dict[str, float] = {}
LAST_ANALYSIS_SIGNALS: List[Dict] = []
LAST_HOURLY_NOTIFICATION_TIME: float = 0.0
LAST_ANALYSIS_ONLY_NOTIFICATION_TIME: float = 0.0
LAST_WEBSHARE_UPLOAD_TIME: float = 0.0 
GLOBAL_MACRO_CONTEXT: Dict = {'fgi_proxy': 0.0, 'fgi_raw_value': 'N/A', 'forex_bonus': 0.0}
IS_FIRST_MAIN_LOOP_COMPLETED: bool = False 
OPEN_POSITIONS: List[Dict] = [] # 現在保有中のポジション (SL/TP監視用)
ACCOUNT_EQUITY_USDT: float = 0.0 # 現時点での総資産 (リスク計算に使用)

if TEST_MODE:
    logging.warning("⚠️ WARNING: TEST_MODE is active. Trading is disabled.")

# CCXTクライアントの準備完了フラグ
IS_CLIENT_READY: bool = False

# 取引ルール設定
# 12時間に修正 (7200 -> 43200) - Patch 61
TRADE_SIGNAL_COOLDOWN = 60 * 60 * 12 
SIGNAL_THRESHOLD = 0.65             
TOP_SIGNAL_COUNT = 1                # ★ 常に1銘柄のみ取引試行 (Patch 59で導入)
REQUIRED_OHLCV_LIMITS = {'1m': 1000, '5m': 1000, '15m': 1000, '1h': 1000, '4h': 1000} 

# テクニカル分析定数 (v19.0.28ベース)
TARGET_TIMEFRAMES = ['1m', '5m', '15m', '1h', '4h'] 
BASE_SCORE = 0.40                  
LONG_TERM_SMA_LENGTH = 200         
LONG_TERM_REVERSAL_PENALTY = 0.20   
STRUCTURAL_PIVOT_BONUS = 0.05       
RSI_MOMENTUM_LOW = 40              
MACD_CROSS_PENALTY = 0.15          
LIQUIDITY_BONUS_MAX = 0.06          
FGI_PROXY_BONUS_MAX = 0.05         
FOREX_BONUS_MAX = 0.0

# 💡 FGI API設定を追加
FGI_API_URL = "https://api.alternative.me/fng/?limit=1" # Alternative.me API

# ボラティリティ指標 (ATR) の設定 
ATR_LENGTH = 14
ATR_MULTIPLIER_SL = 2.0 # SLをATRの2.0倍に設定 (動的SLのベース)
MIN_RISK_PERCENT = 0.008 # SL幅の最小パーセンテージ (0.8%)

# 市場環境に応じた動的閾値調整のための定数
FGI_SLUMP_THRESHOLD = -0.02         
FGI_ACTIVE_THRESHOLD = 0.02         
SIGNAL_THRESHOLD_SLUMP = 0.85       
SIGNAL_THRESHOLD_NORMAL = 0.80      
SIGNAL_THRESHOLD_ACTIVE = 0.75      

RSI_DIVERGENCE_BONUS = 0.10         
VOLATILITY_BB_PENALTY_THRESHOLD = 0.01 
OBV_MOMENTUM_BONUS = 0.04           

# FastAPIアプリケーションの初期化
app = FastAPI(
    title="Apex Crypto Bot API",
    description="CCXTを利用した自動取引ボットのFastAPIインターフェース",
    version="v20.0.25"
)

# ====================================================================================
# UTILITIES & FORMATTING 
# ====================================================================================

def format_usdt(amount: float) -> str:
    """USDT金額を整形する"""
    if amount is None:
        amount = 0.0
        
    if amount >= 1.0:
        return f"{amount:,.2f}"
    elif amount >= 0.01:
        return f"{amount:.4f}"
    else:
        return f"{amount:.6f}"
        
def format_price(price: float) -> str:
    """価格を整形する"""
    if price is None:
        price = 0.0
    # 0.01より大きい場合は小数点以下2桁、それ以外は動的に
    if price >= 0.01:
        return f"{price:,.2f}"
    return f"{price:,.8f}".rstrip('0').rstrip('.')

# 清算価格の計算関数
def calculate_liquidation_price(entry_price: float, leverage: int, side: str = 'long', maintenance_margin_rate: float = MIN_MAINTENANCE_MARGIN_RATE) -> float:
    """
    指定されたエントリー価格、レバレッジ、維持証拠金率に基づき、
    推定清算価格 (Liquidation Price) を計算する。
    """
    if leverage <= 0 or entry_price <= 0:
        return 0.0
        
    # 必要証拠金率 (1 / Leverage)
    initial_margin_rate = 1 / leverage
    
    if side.lower() == 'long':
        # ロングの場合、価格下落で清算
        liquidation_price = entry_price * (1 - initial_margin_rate + maintenance_margin_rate)
    elif side.lower() == 'short':
        # ショートの場合、価格上昇で清算
        liquidation_price = entry_price * (1 + initial_margin_rate - maintenance_margin_rate)
    else:
        return 0.0
        
    return max(0.0, liquidation_price) # 価格は0未満にはならない

def get_estimated_win_rate(score: float) -> str:
    """スコアに基づいて推定勝率を返す (通知用)"""
    if score >= 0.90: return "90%+"
    if score >= 0.85: return "85-90%"
    if score >= 0.75: return "75-85%"
    if score >= 0.65: return "65-75%" 
    if score >= 0.60: return "60-65%"
    return "<60% (低)"

def get_current_threshold(macro_context: Dict) -> float:
    """現在の市場環境に合わせた動的な取引閾値を決定し、返す。"""
    global FGI_SLUMP_THRESHOLD, FGI_ACTIVE_THRESHOLD
    global SIGNAL_THRESHOLD_SLUMP, SIGNAL_THRESHOLD_NORMAL, SIGNAL_THRESHOLD_ACTIVE
    
    fgi_proxy = macro_context.get('fgi_proxy', 0.0)
    
    if fgi_proxy < FGI_SLUMP_THRESHOLD:
        return SIGNAL_THRESHOLD_SLUMP
    
    elif fgi_proxy > FGI_ACTIVE_THRESHOLD:
        return SIGNAL_THRESHOLD_ACTIVE
        
    else:
        return SIGNAL_THRESHOLD_NORMAL

def get_score_breakdown(signal: Dict) -> str:
    """分析スコアの詳細なブレークダウンメッセージを作成する (Telegram通知用)"""
    # ロジックの加点/減点要因は維持
    tech_data = signal.get('tech_data', {})
    timeframe = signal.get('timeframe', 'N/A')
    side = signal.get('side', 'long') # Long/Shortを判定
    
    LONG_TERM_REVERSAL_PENALTY_CONST = LONG_TERM_REVERSAL_PENALTY 
    MACD_CROSS_PENALTY_CONST = MACD_CROSS_PENALTY                 
    LIQUIDITY_BONUS_POINT_CONST = LIQUIDITY_BONUS_MAX           
    
    breakdown_list = []

    # 1. ベーススコア
    breakdown_list.append(f"  - **ベーススコア ({timeframe})**: <code>+{BASE_SCORE*100:.1f}</code> 点")
    
    # 2. 長期トレンド/構造の確認
    
    penalty_value = tech_data.get('long_term_reversal_penalty_value', 0.0)
    
    if side == 'long':
        if penalty_value > 0.0:
            breakdown_list.append(f"  - ❌ 長期トレンド逆行 (SMA{LONG_TERM_SMA_LENGTH}): <code>-{penalty_value*100:.1f}</code> 点")
        else:
            breakdown_list.append(f"  - ✅ 長期トレンド一致 (SMA{LONG_TERM_SMA_LENGTH}): <code>+{LONG_TERM_REVERSAL_PENALTY_CONST*100:.1f}</code> 点 (ペナルティ回避)")
    else: # Short
        if penalty_value > 0.0:
            breakdown_list.append(f"  - ✅ 長期トレンド一致 (SMA{LONG_TERM_SMA_LENGTH}下): <code>+{penalty_value*100:.1f}</code> 点 (ロングペナルティ回避)")
        else:
            breakdown_list.append(f"  - ❌ 長期トレンド不利: <code>-{LONG_TERM_REVERSAL_PENALTY_CONST*100:.1f}</code> 点相当")

    pivot_bonus = tech_data.get('structural_pivot_bonus', 0.0)
    if pivot_bonus > 0.0:
        breakdown_list.append(f"  - ✅ 価格構造/ピボット支持: <code>+{pivot_bonus*100:.1f}</code> 点")

    # 3. モメンタム/出来高の確認
    macd_penalty_applied = tech_data.get('macd_penalty_value', 0.0)
    total_momentum_penalty = macd_penalty_applied

    if total_momentum_penalty > 0.0:
        breakdown_list.append(f"  - ❌ モメンタム/クロス不利: <code>-{total_momentum_penalty*100:.1f}</code> 点")
    else:
        breakdown_list.append(f"  - ✅ MACD/RSIモメンタム加速: <code>+{MACD_CROSS_PENALTY_CONST*100:.1f}</code> 点相当 (ペナルティ回避)")

    obv_bonus = tech_data.get('obv_momentum_bonus_value', 0.0)
    if obv_bonus > 0.0:
        breakdown_list.append(f"  - ✅ 出来高/OBV確証: <code>+{obv_bonus*100:.1f}</code> 点")
    
    # 4. 流動性/マクロ要因
    liquidity_bonus = tech_data.get('liquidity_bonus_value', 0.0)
    if liquidity_bonus > 0.0:
        breakdown_list.append(f"  - ✅ 流動性 (板の厚み) 優位: <code>+{LIQUIDITY_BONUS_POINT_CONST*100:.1f}</code> 点")
        
    fgi_bonus = tech_data.get('sentiment_fgi_proxy_bonus', 0.0)
    if abs(fgi_bonus) > 0.001:
        sign = '✅' if fgi_bonus > 0 else '❌'
        # ロング: fgi_proxy > 0でボーナス、ショート: fgi_proxy < 0でボーナス
        is_fgi_favorable = (side == 'long' and fgi_bonus > 0) or (side == 'short' and fgi_bonus < 0)
        
        breakdown_list.append(f"  - {sign} FGIマクロ影響 ({side.upper()}方向): <code>{'+' if fgi_bonus > 0 else ''}{fgi_bonus*100:.1f}</code> 点")

    forex_bonus = tech_data.get('forex_bonus', 0.0) 
    breakdown_list.append(f"  - ⚪ 為替マクロ影響: <code>{forex_bonus*100:.1f}</code> 点 (機能削除済)")
    
    volatility_penalty = tech_data.get('volatility_penalty_value', 0.0)
    if volatility_penalty < 0.0:
        breakdown_list.append(f"  - ❌ ボラティリティ過熱ペナルティ: <code>{volatility_penalty*100:.1f}</code> 点")

    return "\n".join(breakdown_list)

def format_startup_message(
    account_status: Dict, 
    macro_context: Dict, 
    monitoring_count: int,
    current_threshold: float,
    bot_version: str = "v20.0.25" # バージョンを更新
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
        
    trade_status = "自動売買 **ON** (Long/Short)" if not TEST_MODE else "自動売買 **OFF** (TEST_MODE)" # ショート対応を追記

    header = (
        f"🤖 **Apex BOT 起動完了通知** 🟢\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - **確認日時**: {now_jst} (JST)\n"
        f"  - **取引所**: <code>{CCXT_CLIENT_NAME.upper()}</code> (先物モード / **{LEVERAGE}x**)\n" 
        f"  - **自動売買**: <b>{trade_status}</b>\n"
        f"  - **取引ロット**: **リスクベースサイジング**\n" 
        f"  - **最大リスク/取引**: <code>{MAX_RISK_PER_TRADE_PERCENT*100:.2f}</code> %\n" 
        f"  - **監視銘柄数**: <code>{monitoring_count}</code>\n"
        f"  - **BOTバージョン**: <code>{bot_version}</code>\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n\n"
    )

    balance_section = f"💰 <b>先物口座ステータス</b>\n" 
    if account_status.get('error'):
        balance_section += f"<pre>⚠️ ステータス取得失敗 (致命的エラーにより取引停止中)</pre>\n"
    else:
        equity_display = account_status['total_usdt_balance'] # equity (総資産)として扱う
        balance_section += (
            f"  - **総資産 (Equity)**: <code>{format_usdt(equity_display)}</code> USDT\n" 
        )
        
        # ボットが管理しているポジション
        if OPEN_POSITIONS:
            # filled_usdt は先物では名目価値 (Notional Value)
            total_managed_value = sum(p['filled_usdt'] for p in OPEN_POSITIONS) 
            balance_section += (
                f"  - **管理中ポジション**: <code>{len(OPEN_POSITIONS)}</code> 銘柄 (名目価値合計: <code>{format_usdt(total_managed_value)}</code> USDT)\n" 
            )
            for i, pos in enumerate(OPEN_POSITIONS[:3]): # Top 3のみ表示
                base_currency = pos['symbol'].replace('/USDT', '')
                side_tag = '🟢L' if pos.get('side', 'long') == 'long' else '🔴S' # ショート対応
                balance_section += f"    - Top {i+1}: {base_currency} ({side_tag}, SL: {format_price(pos['stop_loss'])} / TP: {format_price(pos['take_profit'])})\n"
            if len(OPEN_POSITIONS) > 3:
                balance_section += f"    - ...他 {len(OPEN_POSITIONS) - 3} 銘柄\n"
        else:
             balance_section += f"  - **管理中ポジション**: <code>なし</code>\n"

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
    """Telegram通知用のメッセージを作成する (取引結果を追加)"""
    now_jst = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")
    symbol = signal['symbol']
    timeframe = signal['timeframe']
    score = signal['score']
    side = signal.get('side', 'long') # Long/Shortを判定
    
    # trade_resultから値を取得する場合があるため、get()を使用
    entry_price = signal.get('entry_price', trade_result.get('entry_price', 0.0) if trade_result else 0.0)
    stop_loss = signal.get('stop_loss', trade_result.get('stop_loss', 0.0) if trade_result else 0.0)
    take_profit = signal.get('take_profit', trade_result.get('take_profit', 0.0) if trade_result else 0.0)
    liquidation_price = signal.get('liquidation_price', 0.0)
    rr_ratio = signal.get('rr_ratio', 0.0)
    estimated_wr = get_estimated_win_rate(score)
    breakdown_details = get_score_breakdown(signal) if context != "ポジション決済" else ""
    
    trade_section = ""
    trade_status_line = ""
    
    # リスク幅、リワード幅の計算をLong/Shortで反転
    risk_width = abs(entry_price - stop_loss)
    reward_width = abs(take_profit - entry_price)

    if context == "取引シグナル":
        lot_size_units = signal.get('lot_size_units', 0.0) # 数量 (単位)
        notional_value = signal.get('notional_value', 0.0) # 名目価値
        trade_type_text = "先物ロング" if side == 'long' else "先物ショート"
        order_type_text = "成行買い" if side == 'long' else "成行売り"
        
        if TEST_MODE:
            trade_status_line = f"⚠️ **テストモード**: 取引は実行されません。(ロット: {format_usdt(notional_value)} USDT, {LEVERAGE}x)"
        elif trade_result is None or trade_result.get('status') == 'error':
            error_msg = trade_result.get('error_message', 'APIエラー') if trade_result else '注文実行エラー'
            trade_status_line = f"❌ **自動売買 失敗**: {error_msg}"
        elif trade_result.get('status') == 'ok':
            trade_status_line = f"✅ **自動売買 成功**: **{trade_type_text}**注文を執行しました。"
            
        filled_amount_raw = trade_result.get('filled_amount', 0.0)
        # trade_resultから取得した値が文字列（取引所APIからの応答でよくある形式）の場合にfloatに変換する
        try:
            filled_amount = float(filled_amount_raw)
        except (ValueError, TypeError):
            filled_amount = 0.0
            
        filled_usdt_notional = trade_result.get('filled_usdt', 0.0)
        risk_usdt = signal.get('risk_usdt', 0.0) # リスク額
        
        trade_section = (
            f"💰 **取引実行結果**\n"
            f" - **注文タイプ**: <code>先物 (Future) / {order_type_text} ({side.capitalize()})</code>\n"
            f" - **レバレッジ**: <code>{LEVERAGE}</code> 倍\n"
            f" - **リスク許容額**: <code>{format_usdt(risk_usdt)}</code> USDT (総資産の {MAX_RISK_PER_TRADE_PERCENT*100:.2f} %)\n"
            f" - **約定ロット**: <code>{filled_amount:.4f}</code> 単位 (約 <code>{format_usdt(filled_usdt_notional)}</code> USDT相当)\n"
            f" - **約定価格**: <code>{format_price(entry_price)}</code>\n"
            f" - **清算価格**: <code>{format_price(liquidation_price)}</code>\n"
        )
        
    elif context == "ポジション決済":
        trade_status_line = f"💰 **ポジション決済通知**"
        
        profit_usdt = trade_result.get('profit_usdt', 0.0)
        profit_rate = trade_result.get('profit_rate', 0.0)
        
        exit_type_map = {
            'SL': '🔴 損切り (Stop Loss)',
            'TP': '🟢 利確 (Take Profit)',
            'MKT_EXIT': '🟡 強制決済 (Market Exit)'
        }
        exit_text = exit_type_map.get(exit_type, '⚪ 通常決済')

        trade_section = (
            f"🔒 **決済詳細**\n"
            f" - **決済種別**: {exit_text}\n"
            f" - **決済価格**: <code>{format_price(trade_result.get('exit_price', 0.0))}</code>\n"
            f" - **損益額**: <code>{'+' if profit_usdt >= 0 else ''}{format_usdt(profit_usdt)}</code> USDT\n"
            f" - **損益率**: <code>{'+' if profit_rate >= 0 else ''}{profit_rate*100:.2f}</code> %\n"
            f" - **保有期間**: <code>{trade_result.get('holding_period', 'N/A')}</code>\n"
        )


    header = (
        f"{trade_status_line}\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - **時刻**: {now_jst} (JST)\n"
        f"  - **銘柄**: <code>{symbol}</code>\n"
        f"  - **方向**: <b>{'🟢 ロング' if side == 'long' else '🔴 ショート'}</b> (TF: {timeframe})\n" 
        f"  - **スコア**: <code>{score*100:.2f} / 100</code> (推定勝率: {estimated_wr})\n"
        f"  - **R:R比**: <code>{rr_ratio:.2f}</code> (リスク幅: {format_price(risk_width)}, リワード幅: {format_price(reward_width)})\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
    )

    sl_tp_section = (
        f"🛡️ **リスク管理 (SL/TP)**\n"
        f" - **損切り (SL)**: <code>{format_price(stop_loss)}</code>\n"
        f" - **利確 (TP)**: <code>{format_price(take_profit)}</code>\n"
        f" - **市場閾値**: <code>{current_threshold*100:.0f} / 100</code>\n"
    )
    
    breakdown_section = f"\n📊 **スコア内訳**\n{breakdown_details}\n" if breakdown_details else ""
    
    footer = (
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"<pre>※ Telegramの通知は非同期のため、注文実行時刻と通知時刻がわずかにずれることがあります。</pre>"
    )

    return header + trade_section + sl_tp_section + breakdown_section + footer

def format_daily_analysis_message(signals: List[Dict], current_threshold: float, monitoring_count: int) -> str:
    """定期分析専用通知用のメッセージを作成する"""
    now_jst = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")
    
    if signals:
        top_signals_text = "✨ <b>検出されたシグナル TOP 3</b>\n"
        for i, signal in enumerate(signals[:3]):
            side = signal.get('side', 'long')
            score = signal['score']
            timeframe = signal['timeframe']
            
            top_signals_text += (
                f"  - **{i+1}.** <code>{signal['symbol']}</code> ({'🟢L' if side == 'long' else '🔴S'}, TF: {timeframe}, Score: {score*100:.2f})\n"
            )
        
        if len(signals) > 3:
            top_signals_text += f"  - ...他 {len(signals) - 3} 銘柄\n"
            
    else:
        top_signals_text = "✅ **良好なシグナルは検出されませんでした。**\n"

    message = (
        f"📈 **Apex BOT 定期分析通知** 📊\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - **時刻**: {now_jst} (JST)\n"
        f"  - **監視銘柄数**: <code>{monitoring_count}</code>\n"
        f"  - **現在の取引閾値**: <code>{current_threshold*100:.0f} / 100</code>\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n\n"
        f"{top_signals_text}\n"
        f"<pre>※ この通知は取引の実行とは関係なく、定期的な分析結果を報告するものです。</pre>"
    )
    return message

# ====================================================================================
# API COMMUNICATIONS (ASYNC CCXT & TELEGRAM)
# ====================================================================================

async def telegram_send_message(message: str) -> bool:
    """Telegramにメッセージを送信する (HTMLパーシングを使用)"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.error("Telegram token or chat ID is not set.")
        return False
        
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        'chat_id': TELEGRAM_CHAT_ID,
        'text': message,
        'parse_mode': 'HTML' # HTMLタグを有効にする
    }
    
    # requestsは同期処理なので、ThreadPoolExecutorで実行するか、aiohttpに切り替える必要があるが、
    # シンプル化のため、ここでは同期処理をそのまま使用し、awaitを付けない。
    # BOTのメインロジックの実行に影響を与えないよう、メインループ外で実行するのが望ましい。
    # ccxt.async_supportを使っているため、requestsの実行はメインループをブロックしないよう注意。
    # 通常、FastAPIの@app.on_event("startup")内のasyncio.create_taskで実行されるため、ここでは同期requestsを許容する。
    try:
        response = requests.post(url, data=payload, timeout=5)
        response.raise_for_status()
        if response.status_code == 200:
            logging.info("Telegram notification sent successfully.")
            return True
        else:
            logging.error(f"Telegram API error: {response.text}")
            return False
    except requests.exceptions.RequestException as e:
        logging.error(f"Failed to send Telegram message: {e}")
        return False

async def upload_log_to_webshare(log_data: str) -> bool:
    """WebShareサービスへログデータをPOSTする"""
    if WEBSHARE_METHOD != "HTTP" or not WEBSHARE_POST_URL:
        logging.info("WebShare HTTP upload is disabled or not configured.")
        return False

    logging.info(f"Uploading log data to WebShare at {WEBSHARE_POST_URL}")
    try:
        # requestsは同期I/Oであるため、asyncio.to_threadを使用してスレッドプールで実行し、ブロックを避ける
        response = await asyncio.to_thread(
            requests.post, 
            WEBSHARE_POST_URL, 
            json={"data": log_data}, 
            timeout=10
        )
        response.raise_for_status() # HTTPエラーを発生させる
        
        logging.info(f"WebShare upload successful. Status: {response.status_code}")
        return True
    except requests.exceptions.RequestException as e:
        logging.error(f"WebShare upload failed: {e}")
        return False
    except Exception as e:
        logging.error(f"An unexpected error occurred during WebShare upload: {e}")
        return False

async def update_market_symbols_async() -> Tuple[List[str], Optional[Dict]]:
    """取引所の市場リストを取得し、出来高順にソートして監視シンボルを更新する"""
    global EXCHANGE_CLIENT, CURRENT_MONITOR_SYMBOLS
    
    if not EXCHANGE_CLIENT:
        logging.error("Exchange client not initialized for market update.")
        return DEFAULT_SYMBOLS, None
        
    try:
        # 市場情報の取得
        markets = await EXCHANGE_CLIENT.load_markets()
        
        # 先物取引 (Future/Swap) のUSDTペアのみを抽出
        futures_markets = {
            symbol: market for symbol, market in markets.items()
            if market.get('contract') and market.get('quote') == 'USDT'
        }
        
        if not futures_markets:
            logging.warning("No USDT-based future/swap markets found.")
            # 代替案として、デフォルトシンボルリストを先物形式に変換して使用
            futures_symbols = [s.replace('/USDT', '/USDT:USDT') for s in DEFAULT_SYMBOLS]
            return futures_symbols, markets # デフォルトシンボルリストを返す

        # 出来高 (Volume) の取得
        # fetch_tickersは非同期で実行可能
        tickers = await EXCHANGE_CLIENT.fetch_tickers(list(futures_markets.keys()))
        
        # 出来高に基づいてソート
        # 'quoteVolume' (USDT建て出来高) が存在しない場合は0として扱う
        sorted_tickers = sorted(
            tickers.values(),
            key=lambda x: x.get('quoteVolume') or 0,
            reverse=True
        )
        
        # 出来高TOPのシンボルを選出 (シンボルは先物形式: BTC/USDT:USDT など)
        top_symbols = [
            t['symbol'] for t in sorted_tickers 
            if t.get('quoteVolume') and t['quoteVolume'] > 1000000 
            and t['symbol'] in futures_markets
        ][:TOP_SYMBOL_LIMIT]
        
        # デフォルトシンボルに存在し、TOP_SYMBOL_LIMITに含まれていないものを追加
        # (重要銘柄の漏れを防ぐ)
        for default_sym in DEFAULT_SYMBOLS:
            # デフォルトシンボルを先物シンボル形式に変換 (例: BTC/USDT -> BTC/USDT:USDT)
            future_sym = next((s for s in futures_markets.keys() if s.startswith(default_sym)), None)
            if future_sym and future_sym not in top_symbols:
                top_symbols.append(future_sym)
        
        # 最終的な監視リストを更新
        CURRENT_MONITOR_SYMBOLS = top_symbols
        logging.info(f"Updated monitoring list. Total symbols: {len(CURRENT_MONITOR_SYMBOLS)}")
        
        return CURRENT_MONITOR_SYMBOLS, markets
        
    except ccxt.NetworkError as e:
        logging.error(f"Market update Network Error: {e}")
        return CURRENT_MONITOR_SYMBOLS, None
    except ccxt.ExchangeError as e:
        logging.error(f"Market update Exchange Error: {e}")
        return CURRENT_MONITOR_SYMBOLS, None
    except Exception as e:
        logging.error(f"Market update failed unexpectedly: {e}")
        return CURRENT_MONITOR_SYMBOLS, None

async def get_historical_ohlcv(symbol: str, timeframe: str, limit: int) -> Optional[pd.DataFrame]:
    """指定されたシンボルのOHLCVデータを取得し、DataFrameとして返す (NaN修正済み)"""
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT:
        return None
        
    try:
        # fetch_ohlcvは非同期で実行
        ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(symbol, timeframe, limit=limit)
        
        if not ohlcv:
            logging.warning(f"No OHLCV data fetched for {symbol} ({timeframe}).")
            return None
            
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True).dt.tz_convert(JST)
        df.set_index('timestamp', inplace=True)
        
        # 修正点: データ分析の安定性向上のため、NaNを含む行を削除
        df.dropna(inplace=True)
        
        if df.empty:
            logging.warning(f"OHLCV data for {symbol} ({timeframe}) became empty after dropping NaNs.")
            return None
            
        return df
        
    except ccxt.NetworkError as e:
        logging.warning(f"Network Error fetching OHLCV for {symbol} ({timeframe}): {e}")
    except ccxt.ExchangeError as e:
        logging.warning(f"Exchange Error fetching OHLCV for {symbol} ({timeframe}): {e}")
    except Exception as e:
        logging.error(f"Unexpected error fetching OHLCV for {symbol} ({timeframe}): {e}")
        
    return None

async def initialize_exchange() -> Dict:
    """CCXTクライアントを初期化し、先物設定と残高を取得する"""
    global EXCHANGE_CLIENT, IS_CLIENT_READY, ACCOUNT_EQUITY_USDT
    
    client_class = getattr(ccxt_async, CCXT_CLIENT_NAME.lower(), None)
    if not client_class:
        logging.error(f"Exchange client '{CCXT_CLIENT_NAME}' not supported by CCXT.")
        return {'error': True, 'message': 'Exchange not supported'}

    try:
        # クライアントの初期化 (先物取引モードを設定)
        EXCHANGE_CLIENT = client_class({
            'apiKey': API_KEY,
            'secret': SECRET_KEY,
            'enableRateLimit': True, # レート制限を有効化
            'options': {
                'defaultType': TRADE_TYPE, # 'future' or 'swap'
            },
        })
        
        # ロードマーケットを実行し、取引所との接続を確認
        await EXCHANGE_CLIENT.load_markets()
        
        # アカウント残高の取得
        account_status = await fetch_account_status_async()
        
        if account_status.get('error'):
            raise Exception(account_status.get('message', 'Failed to fetch initial account status'))

        ACCOUNT_EQUITY_USDT = account_status.get('total_usdt_balance', 0.0)
        
        # クライアントの準備完了フラグを立てる
        IS_CLIENT_READY = True
        logging.info(f"CCXT Client '{CCXT_CLIENT_NAME}' initialized for {TRADE_TYPE} trading.")
        
        return account_status
        
    except Exception as e:
        logging.critical(f"Client initialization failed (Fatal): {e}")
        IS_CLIENT_READY = False
        return {'error': True, 'message': str(e)}
        
async def fetch_account_status_async() -> Dict:
    """アカウントの残高、ポジション情報を取得し、グローバル変数に反映する"""
    global EXCHANGE_CLIENT, ACCOUNT_EQUITY_USDT, OPEN_POSITIONS
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        return {'error': True, 'message': 'Client not ready'}

    try:
        # 1. 残高の取得
        balance = await EXCHANGE_CLIENT.fetch_balance()
        
        # USDT (または統一証拠金) の総資産 (equity) を取得
        # CCXTは unified account の場合、'total' や 'equity' に総資産を格納することが多い
        total_balance = balance.get('total', {}).get('USDT', 0.0)
        
        # total_balanceが0.0の場合、先物口座の equity/total を確認
        if total_balance == 0.0:
            if 'info' in balance and 'totalEquity' in balance['info']:
                # MEXCなどの場合
                total_balance = float(balance['info']['totalEquity'])
            elif 'equity' in balance:
                # 汎用的なequity
                total_balance = balance['equity'].get('USDT', 0.0)

        # グローバル変数の更新
        ACCOUNT_EQUITY_USDT = total_balance
        
        # 2. ポジション情報の取得
        # fetch_positions は先物/マージン取引でのみ利用可能
        positions_raw = await EXCHANGE_CLIENT.fetch_positions()
        
        # USDT先物ポジションのみをフィルタリング
        new_open_positions = []
        for pos in positions_raw:
            # positionSide が 'long' または 'short' で、数量 (contracts) が0でないものを抽出
            # symbolが 'USDT'で終わるものを対象とする
            if pos['symbol'] in CURRENT_MONITOR_SYMBOLS and pos.get('contracts', 0) != 0 and pos.get('notional', 0) != 0:
                # ボットが管理するために必要な情報を抽出
                new_open_positions.append({
                    'symbol': pos['symbol'],
                    'id': str(uuid.uuid4()), # 管理用IDを生成 (SL/TP監視用)
                    'side': 'long' if pos['side'] == 'long' else 'short', # CCXTのsideを統一
                    'entry_price': pos['entryPrice'],
                    'contracts': pos['contracts'], # 数量 (単位)
                    'filled_usdt': pos['notional'], # 名目価値 (USDT相当)
                    'leverage': pos.get('leverage', LEVERAGE),
                    # SL/TPはまだ設定されていないとして初期化 (監視タスクで更新)
                    'stop_loss': 0.0, 
                    'take_profit': 0.0,
                    'liquidation_price': calculate_liquidation_price(
                        pos['entryPrice'], 
                        pos.get('leverage', LEVERAGE), 
                        'long' if pos['side'] == 'long' else 'short'
                    ),
                    'timestamp': time.time(), # ポジション取得時刻
                })
        
        # グローバル変数の更新
        OPEN_POSITIONS = new_open_positions
        
        return {
            'error': False, 
            'total_usdt_balance': ACCOUNT_EQUITY_USDT,
            'open_positions_count': len(OPEN_POSITIONS),
            'message': 'Account status fetched successfully'
        }
        
    except ccxt.NetworkError as e:
        logging.error(f"Network Error fetching account status: {e}")
        return {'error': True, 'message': 'Network Error'}
    except ccxt.ExchangeError as e:
        logging.error(f"Exchange Error fetching account status: {e}")
        return {'error': True, 'message': 'Exchange Error'}
    except Exception as e:
        logging.error(f"Unexpected error fetching account status: {e}")
        return {'error': True, 'message': f'Unexpected Error: {e}'}

async def get_macro_context_async() -> Dict:
    """FGI (Fear & Greed Index)などのマクロ情報を取得し、取引への影響度を計算する"""
    fgi_proxy = 0.0
    fgi_raw_value = 'N/A'
    forex_bonus = 0.0
    
    try:
        # FGI (同期 requestsのためasyncio.to_threadを使用)
        response = await asyncio.to_thread(requests.get, FGI_API_URL, timeout=5)
        response.raise_for_status()
        data = response.json()
        
        fgi_data = data.get('data', [{}])[0]
        fgi_value = int(fgi_data.get('value', 50))
        fgi_raw_value = f"{fgi_value} ({fgi_data.get('value_classification', 'Neutral')})"
        
        # FGIを-1.0から+1.0の範囲に正規化し、Proxyスコアを計算
        # -100(Extreme Fear) -> -1.0, 100(Extreme Greed) -> +1.0
        # 0.5で割ることで、-1.0〜1.0の範囲になる
        normalized_fgi = (fgi_value - 50) / 50.0 
        
        # FGIが強い恐怖または強い貪欲にある場合に影響を付与 (最大±FGI_PROXY_BONUS_MAX)
        fgi_proxy = normalized_fgi * FGI_PROXY_BONUS_MAX 
        
        # 為替マクロ (FOREX_BONUS_MAXは現在0.0で機能削除済)
        forex_bonus = 0.0
        
    except Exception as e:
        logging.warning(f"Failed to fetch or process FGI data: {e}. Using default neutral context.")
        fgi_proxy = 0.0
        fgi_raw_value = 'N/A'
        
    return {
        'fgi_proxy': fgi_proxy, 
        'fgi_raw_value': fgi_raw_value, 
        'forex_bonus': forex_bonus, # 0.0
        'error': False
    }

async def get_market_liquidity_async(symbol: str) -> Dict:
    """指定されたシンボルの板情報から流動性スコアを計算する"""
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT:
        return {'error': True, 'liquidity_bonus': 0.0}

    try:
        # 1. 板情報の取得 (深度100まで)
        orderbook = await EXCHANGE_CLIENT.fetch_order_book(symbol, limit=100)
        
        bids = orderbook['bids']
        asks = orderbook['asks']
        
        # 2. トップ板の厚みを計算
        # トップ10のBidとAskの名目価値 (Notional Value) を合計
        top_n = 10
        top_bids_value = sum(price * amount for price, amount in bids[:top_n])
        top_asks_value = sum(price * amount for price, amount in asks[:top_n])
        
        # 3. 流動性スコアの計算
        # 流動性が高いほど (名目価値が大きいほど) ボーナスを付与
        # ここでは、Bid/Askの小さい方（より確実に約定できる側）を基準とする
        min_top_value = min(top_bids_value, top_asks_value)
        
        # 例: 1,000,000 USDT以上の流動性で最大ボーナス (LIQUIDITY_BONUS_MAX)
        max_value_for_max_bonus = 1_000_000.0
        
        # 流動性ボーナスは最大LIQUIDITY_BONUS_MAX
        liquidity_bonus = min(
            min_top_value / max_value_for_max_bonus * LIQUIDITY_BONUS_MAX, 
            LIQUIDITY_BONUS_MAX
        )
        
        return {
            'error': False, 
            'liquidity_bonus': liquidity_bonus,
            'top_bid_value': top_bids_value,
            'top_ask_value': top_asks_value,
        }
        
    except ccxt.NetworkError:
        logging.warning(f"Network Error fetching order book for {symbol}")
    except ccxt.ExchangeError as e:
        # Code 30005 (流動性不足)やその他のエラーをここで検出する
        if '30005' in str(e):
             logging.warning(f"Order book for {symbol} has insufficient liquidity (Code 30005). Skipping.")
        else:
             logging.warning(f"Exchange Error fetching order book for {symbol}: {e}")
        return {'error': True, 'liquidity_bonus': 0.0, 'error_code': 'EXCHANGE_ERROR'}
    except Exception as e:
        logging.error(f"Unexpected error fetching market liquidity for {symbol}: {e}")

    return {'error': True, 'liquidity_bonus': 0.0, 'error_code': 'UNKNOWN'}

# ====================================================================================
# TECHNICAL ANALYSIS & SIGNAL GENERATION 
# ====================================================================================

def technical_analysis_logic(
    df: pd.DataFrame, 
    symbol: str, 
    timeframe: str, 
    liquidity_bonus_value: float,
    fgi_proxy_bonus: float,
    forex_bonus: float,
) -> Dict:
    """
    OHLCVデータに基づき、複数のテクニカル指標を計算し、統合的なスコアと売買方向を決定する。
    """
    
    # 1. 必要なテクニカル指標の計算
    
    # Simple Moving Average (SMA)
    df['SMA_200'] = ta.sma(df['close'], length=LONG_TERM_SMA_LENGTH)
    df['SMA_50'] = ta.sma(df['close'], length=50)
    
    # Relative Strength Index (RSI)
    df['RSI'] = ta.rsi(df['close'], length=14)
    
    # Moving Average Convergence Divergence (MACD)
    macd_result = ta.macd(df['close'], fast=12, slow=26, signal=9)
    df = df.join(macd_result)
    
    # Bollinger Bands (BBANDS)
    bbands_result = ta.bbands(df['close'], length=20, std=2)
    df = df.join(bbands_result)
    
    # Average True Range (ATR)
    df['ATR'] = ta.atr(df['high'], df['low'], df['close'], length=ATR_LENGTH)
    
    # On Balance Volume (OBV)
    df['OBV'] = ta.obv(df['close'], df['volume'])
    
    # 2. 最新データの取得とNaNチェック
    last_row = df.iloc[-1]
    
    if last_row.isnull().any():
        logging.warning(f"{symbol} ({timeframe}) - Latest data contains NaN after TA calculation. Skipping.")
        return {'score': 0.0, 'side': 'none', 'tech_data': {'error': 'NaN_in_latest_data'}}

    # 3. 構造的なピボット/支持抵抗の確認 (ここでは簡略化のためSMAをピボットとして利用)
    current_close = last_row['close']
    structural_pivot_bonus = 0.0
    
    # 50期間SMAが主要な支持抵抗として機能しているか
    if abs(current_close - last_row['SMA_50']) < last_row['ATR'] * 0.5:
        structural_pivot_bonus = STRUCTURAL_PIVOT_BONUS
    
    # 4. 長期トレンドとの乖離 (ペナルティ計算)
    long_term_reversal_penalty_value = 0.0
    
    # 価格が200SMAより下にある場合、ロングにペナルティ
    is_price_below_200sma = current_close < last_row['SMA_200']
    
    # 5. モメンタムとクロスオーバーの確認
    macd_penalty_value = 0.0
    obv_momentum_bonus_value = 0.0
    
    # MACDのクロス
    is_macd_positive = last_row[f'MACDh_12_26_9'] > 0 # MACDヒストグラムが正
    is_macd_negative = last_row[f'MACDh_12_26_9'] < 0 # MACDヒストグラムが負
    
    # RSIのモメンタム
    is_rsi_low_momentum = last_row['RSI'] < RSI_MOMENTUM_LOW
    is_rsi_high_momentum = last_row['RSI'] > (100 - RSI_MOMENTUM_LOW) # 60以上

    # OBVの傾き (単純な差分で代用)
    obv_change = last_row['OBV'] - df['OBV'].iloc[-2] if len(df) >= 2 else 0
    
    # 6. ボラティリティ過熱ペナルティ
    volatility_penalty_value = 0.0
    # 価格がボリンジャーバンドの上限/下限に触れている場合、ボラティリティが過熱していると見なしペナルティ
    bb_width_percent = (last_row['BBU_20_2.0'] - last_row['BBL_20_2.0']) / last_row['BBM_20_2.0']
    
    # BBANDSの幅が大きすぎる場合 (例: ATRの2倍以上)
    if bb_width_percent > VOLATILITY_BB_PENALTY_THRESHOLD * 2: # 例: 2%以上
        volatility_penalty_value = -0.05
    
    
    # =================================================================
    # 7. スコアリング (方向性の決定と加減算)
    # =================================================================
    
    final_score = BASE_SCORE
    trade_side = 'none'
    
    # --- Long シグナル ---
    long_score = BASE_SCORE
    
    # 1. 長期トレンド: 200SMAより上 (+0.20)
    if not is_price_below_200sma: 
        long_score += LONG_TERM_REVERSAL_PENALTY 
    else: 
        long_term_reversal_penalty_value = LONG_TERM_REVERSAL_PENALTY
        long_score -= LONG_TERM_REVERSAL_PENALTY # ペナルティ適用
        
    # 2. MACDモメンタム: MACDがゼロラインより上、またはポジティブなクロスを維持 (+0.15)
    if is_macd_positive and not is_rsi_low_momentum:
        long_score += MACD_CROSS_PENALTY 
    else:
        macd_penalty_value = MACD_CROSS_PENALTY
        long_score -= MACD_CROSS_PENALTY # ペナルティ適用
        
    # 3. 構造的ピボット (+0.05)
    long_score += structural_pivot_bonus
    
    # 4. OBV: 出来高が価格上昇を裏付けている (+0.04)
    if obv_change > 0:
        obv_momentum_bonus_value = OBV_MOMENTUM_BONUS
        long_score += OBV_MOMENTUM_BONUS
        
    # 5. 流動性/マクロ要因 (+0.06 + FGI + FOREX)
    long_score += liquidity_bonus_value 
    
    sentiment_fgi_proxy_bonus = 0.0
    if fgi_proxy_bonus > 0:
        # FGIがポジティブな場合のみ、ロングにボーナス
        sentiment_fgi_proxy_bonus = fgi_proxy_bonus
    else:
        # FGIがネガティブな場合はペナルティ
        sentiment_fgi_proxy_bonus = fgi_proxy_bonus
        
    long_score += sentiment_fgi_proxy_bonus
    long_score += forex_bonus
    
    # 6. ボラティリティペナルティ
    long_score += volatility_penalty_value


    # --- Short シグナル (Longの逆) ---
    short_score = BASE_SCORE
    
    # 1. 長期トレンド: 200SMAより下 (+0.20)
    if is_price_below_200sma:
        short_score += LONG_TERM_REVERSAL_PENALTY
    else:
        # Longと同じペナルティ値を使用
        long_term_reversal_penalty_value = LONG_TERM_REVERSAL_PENALTY
        short_score -= LONG_TERM_REVERSAL_PENALTY 
        
    # 2. MACDモメンタム: MACDがゼロラインより下、またはネガティブなクロスを維持 (+0.15)
    if is_macd_negative and not is_rsi_high_momentum:
        short_score += MACD_CROSS_PENALTY
    else:
        # Longと同じペナルティ値を使用
        macd_penalty_value = MACD_CROSS_PENALTY
        short_score -= MACD_CROSS_PENALTY
        
    # 3. 構造的ピボット (+0.05)
    short_score += structural_pivot_bonus # ピボットはLong/Short共通
    
    # 4. OBV: 出来高が価格下落を裏付けている (+0.04)
    if obv_change < 0:
        obv_momentum_bonus_value = OBV_MOMENTUM_BONUS
        short_score += OBV_MOMENTUM_BONUS
        
    # 5. 流動性/マクロ要因 (+0.06 + FGI + FOREX)
    short_score += liquidity_bonus_value
    
    sentiment_fgi_proxy_bonus_short = 0.0
    if fgi_proxy_bonus < 0:
        # FGIがネガティブな場合のみ、ショートにボーナス
        sentiment_fgi_proxy_bonus_short = abs(fgi_proxy_bonus) # ショートはプラスとして扱う
    else:
        # FGIがポジティブな場合はペナルティ
        sentiment_fgi_proxy_bonus_short = -fgi_proxy_bonus
        
    short_score += sentiment_fgi_proxy_bonus_short
    short_score += forex_bonus
    
    # 6. ボラティリティペナルティ
    short_score += volatility_penalty_value


    # 8. 最終方向の決定
    if long_score > short_score and long_score >= SIGNAL_THRESHOLD:
        final_score = long_score
        trade_side = 'long'
    elif short_score > long_score and short_score >= SIGNAL_THRESHOLD:
        final_score = short_score
        trade_side = 'short'
    else:
        final_score = max(long_score, short_score) # 閾値未満でも高い方のスコアは保持
        trade_side = 'none'

    # 9. SL/TPの設定 (ATRベースの動的SL/TP)
    
    current_atr = last_row['ATR']
    
    # ATRに基づいてSL幅を設定
    sl_distance = current_atr * ATR_MULTIPLIER_SL 
    
    # SL幅の最小パーセンテージチェック
    min_sl_distance = current_close * MIN_RISK_PERCENT 
    sl_distance = max(sl_distance, min_sl_distance)
    
    # R:R比は1.0を基本とする (リスクベースサイジングで調整)
    rr_ratio = 1.0 
    tp_distance = sl_distance * rr_ratio
    
    # SL/TP価格の計算
    if trade_side == 'long':
        stop_loss = current_close - sl_distance
        take_profit = current_close + tp_distance
    elif trade_side == 'short':
        stop_loss = current_close + sl_distance
        take_profit = current_close - tp_distance
    else:
        stop_loss = 0.0
        take_profit = 0.0

    
    # 10. 結果の構築
    result = {
        'symbol': symbol,
        'timeframe': timeframe,
        'side': trade_side,
        'score': final_score,
        'entry_price': current_close,
        'stop_loss': stop_loss,
        'take_profit': take_profit,
        'rr_ratio': rr_ratio, # 1.0
        'tech_data': {
            'close': current_close,
            'atr': current_atr,
            'sma_200': last_row['SMA_200'],
            'long_term_reversal_penalty_value': long_term_reversal_penalty_value,
            'structural_pivot_bonus': structural_pivot_bonus,
            'macd_penalty_value': macd_penalty_value,
            'obv_momentum_bonus_value': obv_momentum_bonus_value,
            'liquidity_bonus_value': liquidity_bonus_value,
            'sentiment_fgi_proxy_bonus': sentiment_fgi_proxy_bonus,
            'forex_bonus': forex_bonus,
            'volatility_penalty_value': volatility_penalty_value
        }
    }
    
    return result

async def analyze_symbol_async(symbol: str, macro_context: Dict) -> List[Dict]:
    """単一のシンボルに対して複数の時間軸で分析を実行する"""
    tasks = []
    
    # 1. 流動性情報の取得 (全時間軸で共通)
    liquidity_result = await get_market_liquidity_async(symbol)
    if liquidity_result.get('error'):
        # Code 10007 (symbol not support api) または Code 30005 (流動性不足) の場合
        logging.warning(f"Skipping {symbol} due to liquidity/API error.")
        return []
    
    liquidity_bonus = liquidity_result['liquidity_bonus']
    
    # 2. 各時間軸のOHLCVデータ取得タスクを作成
    for tf in TARGET_TIMEFRAMES:
        tasks.append(get_historical_ohlcv(symbol, tf, REQUIRED_OHLCV_LIMITS[tf]))
        
    ohlcv_results = await asyncio.gather(*tasks)
    
    signals = []
    
    # 3. 各時間軸のOHLCVデータに対してテクニカル分析を実行
    for i, df in enumerate(ohlcv_results):
        if df is None:
            continue
            
        timeframe = TARGET_TIMEFRAMES[i]
        
        # テクニカル分析 (同期処理)
        analysis_result = technical_analysis_logic(
            df=df, 
            symbol=symbol, 
            timeframe=timeframe,
            liquidity_bonus_value=liquidity_bonus,
            fgi_proxy_bonus=macro_context['fgi_proxy'],
            forex_bonus=macro_context['forex_bonus']
        )
        
        if analysis_result['side'] != 'none' and analysis_result['score'] >= SIGNAL_THRESHOLD:
            # 清算価格の計算を追加
            entry_price = analysis_result['entry_price']
            liquidation_price = calculate_liquidation_price(
                entry_price, 
                LEVERAGE, 
                analysis_result['side']
            )
            analysis_result['liquidation_price'] = liquidation_price
            
            signals.append(analysis_result)
            
    return signals

async def analyze_and_generate_signals(monitoring_symbols: List[str], macro_context: Dict) -> List[Dict]:
    """全監視銘柄に対して非同期で分析を実行し、有効なシグナルを生成する"""
    
    # 1. 各銘柄の分析タスクを生成
    analysis_tasks = [
        analyze_symbol_async(symbol, macro_context)
        for symbol in monitoring_symbols
    ]
    
    # 2. 全タスクを並行実行
    list_of_signals = await asyncio.gather(*analysis_tasks)
    
    # 3. 結果をフラット化 (List[List[Dict]] -> List[Dict])
    all_signals = [signal for signals_list in list_of_signals for signal in signals_list]
    
    # 4. スコアで降順にソート
    all_signals.sort(key=lambda x: x['score'], reverse=True)
    
    return all_signals

def get_trade_sizing_and_params(signal: Dict, entry_price: float, stop_loss: float, min_amount_unit: float, price_precision: int) -> Dict:
    """
    リスクベースのポジションサイジングを行い、取引数量とパラメータを計算する。
    """
    global ACCOUNT_EQUITY_USDT, MAX_RISK_PER_TRADE_PERCENT
    
    # 1. リスク額の計算
    risk_percent = MAX_RISK_PER_TRADE_PERCENT
    risk_usdt = ACCOUNT_EQUITY_USDT * risk_percent
    
    if ACCOUNT_EQUITY_USDT <= 0:
        logging.error("Account equity is 0 or negative. Cannot perform trade sizing.")
        return {'error': True, 'risk_usdt': 0.0, 'lot_size_units': 0.0, 'notional_value': 0.0}

    # 2. 1単位あたりのUSDリスク (ストップロスまでの距離)
    # USD_Risk_per_Unit = abs(Entry_Price - Stop_Loss) * min_amount_unit
    
    # 3. 許容リスク額から、取引数量 (名目価値) を逆算
    # ロットサイズ (USDT名目価値) = (許容リスク額 / (Entry_Price - Stop_Loss)) * Entry_Price
    
    # SLまでの価格変動幅 (ドル)
    price_range_to_sl = abs(entry_price - stop_loss)
    
    if price_range_to_sl <= 0:
        logging.error("SL distance is zero. Cannot calculate trade size.")
        return {'error': True, 'risk_usdt': risk_usdt, 'lot_size_units': 0.0, 'notional_value': 0.0}
    
    # 必要な単位数 (contracts) を計算 (数量の単位はシンボルによる)
    # Required_Contracts = Risk_USDT / (Entry_Price - Stop_Loss)
    required_contracts_raw = risk_usdt / price_range_to_sl
    
    # 名目価値 (USDT) を計算
    # Notional_Value = Required_Contracts * Entry_Price
    notional_value_raw = required_contracts_raw * entry_price
    
    # 4. 取引所要件に合わせた数量の調整
    
    # 数量を取引所の最小単位 (min_amount_unit) に丸める
    # math.floor() を使用して、小さく丸めることでリスクを確実に守る
    # 必要な数量を最小単位で割って、整数部分を取得し、再度最小単位を掛ける
    if min_amount_unit > 0:
        lot_size_units = math.floor(required_contracts_raw / min_amount_unit) * min_amount_unit
    else:
        # min_amount_unitが不明または0の場合、価格精度に合わせて丸める
        # 通常、先物取引ではmin_amount_unitが必須
        lot_size_units = round(required_contracts_raw, price_precision)
        
    # ロットサイズが0になる場合は、リスク許容額が小さすぎる
    if lot_size_units < min_amount_unit:
        logging.warning(f"Calculated lot size {lot_size_units} is less than min amount {min_amount_unit}. Skipping trade.")
        return {'error': True, 'risk_usdt': risk_usdt, 'lot_size_units': 0.0, 'notional_value': 0.0}

    # 最終的な名目価値を再計算
    final_notional_value = lot_size_units * entry_price
    
    # 5. 結果の構築
    return {
        'error': False,
        'risk_usdt': risk_usdt,
        'lot_size_units': lot_size_units, # 実際に注文する単位数
        'notional_value': final_notional_value, # 実際に注文する名目価値
    }

async def check_position_status(symbol: str) -> Optional[Dict]:
    """現在管理中のポジションリストから、指定されたシンボルのポジション情報を取得する"""
    for pos in OPEN_POSITIONS:
        if pos['symbol'] == symbol:
            return pos
    return None

async def execute_trade_logic(signal: Dict) -> Dict:
    """
    シグナルに基づいて取引を実行し、ポジションを管理リストに追加する。
    また、最小取引単位 (Min Amount) と最小名目価値 (Min Notional Value) のチェックを行う。
    """
    global EXCHANGE_CLIENT, OPEN_POSITIONS
    
    symbol = signal['symbol']
    side = signal['side']
    entry_price = signal['entry_price']
    stop_loss = signal['stop_loss']
    take_profit = signal['take_profit']

    # 1. 既存ポジションのチェック
    existing_pos = await check_position_status(symbol)
    if existing_pos:
        logging.warning(f"{symbol}: Position already exists. Skipping trade.")
        return {'symbol': symbol, 'status': 'skipped', 'message': 'Position already exists'}
        
    if TEST_MODE:
        logging.warning(f"{symbol}: TEST_MODE is ON. Trade execution skipped.")
        # テストモードでは、仮想的な取引パラメータを返して通知をシミュレート
        sizing_params = signal.get('sizing_params', {})
        return {
            'symbol': symbol, 
            'status': 'ok', 
            'message': 'Test trade successful',
            'entry_price': entry_price, 
            'filled_amount': sizing_params.get('lot_size_units', 1.0),
            'filled_usdt': sizing_params.get('notional_value', BASE_TRADE_SIZE_USDT),
            'stop_loss': stop_loss,
            'take_profit': take_profit,
        }

    # 2. 取引所情報と最小単位/精度の取得
    market = EXCHANGE_CLIENT.markets.get(symbol)
    if not market:
        logging.error(f"{symbol}: Market info not found.")
        return {'symbol': symbol, 'status': 'error', 'error_message': 'Market info not found'}
        
    # 最小取引単位 (Min Amount / Lot Size)
    min_amount_unit = market['limits']['amount']['min'] or 0.0
    # 最小名目価値 (Min Notional Value) - Patch 71: Code 400の原因
    min_notional_value = market['limits'].get('cost', {}).get('min') or 0.0
    # 数量の精度
    amount_precision = market['precision']['amount'] or 4
    # 価格の精度
    price_precision = market['precision']['price'] or 4


    # 3. ポジションサイジング
    sizing_result = get_trade_sizing_and_params(signal, entry_price, stop_loss, min_amount_unit, amount_precision)
    if sizing_result.get('error'):
        error_msg = 'Trade size calculation failed or lot size too small.'
        logging.error(f"{symbol}: {error_msg}")
        return {'symbol': symbol, 'status': 'error', 'error_message': error_msg}
        
    lot_size_units = sizing_result['lot_size_units'] # 注文単位
    final_notional_value = sizing_result['notional_value'] # 注文名目価値
    
    # 4. 【最小名目価値チェック】 (Patch 71)
    if min_notional_value > 0 and final_notional_value < min_notional_value:
        logging.warning(
            f"{symbol}: Calculated notional value ({format_usdt(final_notional_value)} USDT) is less than "
            f"min notional value ({format_usdt(min_notional_value)} USDT). Skipping trade (Code 400 risk)."
        )
        # シグナルを更新し、通知メッセージでスキップ理由を報告させる
        signal['sizing_params'] = sizing_result 
        return {'symbol': symbol, 'status': 'skipped', 'message': 'Min Notional Value not met', 'entry_price': entry_price}


    # 5. レバレッジの設定 (取引前に実行)
    try:
        # MEXCの場合、レバレッジ設定は必須ではないが、明示的に設定
        # set_leverage は戻り値を返さないことが多いため、単純にawaitする
        await EXCHANGE_CLIENT.set_leverage(LEVERAGE, symbol)
        await asyncio.sleep(LEVERAGE_SETTING_DELAY) # APIレートリミット対策
    except Exception as e:
        # レバレッジ設定失敗は致命的ではないが、警告
        logging.warning(f"{symbol}: Failed to set leverage to {LEVERAGE}x: {e}")


    # 6. 成行注文の実行
    params = {} 
    
    try:
        # 注文方向
        order_side = 'buy' if side == 'long' else 'sell'
        
        # 注文
        order = await EXCHANGE_CLIENT.create_market_order(
            symbol=symbol, 
            side=order_side, 
            amount=lot_size_units, # 単位数
            params=params
        )
        
        # 注文結果の確認 (完全約定を前提とする)
        if order['status'] != 'closed' and order['status'] != 'filled':
            logging.warning(f"{symbol}: Order status is not filled: {order['status']}. Checking position manually.")

        # 7. ポジションリストへの追加 (即座に追加し、監視タスクで更新されるのを待つ)
        
        # orderから約定情報を取得
        filled_amount = order.get('filled', 0.0)
        entry_price_filled = order.get('average', entry_price) # 平均約定価格
        filled_usdt = filled_amount * entry_price_filled

        # 新しいポジションオブジェクトの作成
        new_pos = {
            'symbol': symbol,
            'id': str(uuid.uuid4()),
            'side': side,
            'entry_price': entry_price_filled,
            'contracts': filled_amount,
            'filled_usdt': filled_usdt,
            'leverage': LEVERAGE,
            'stop_loss': stop_loss, # シグナルから取得したSL/TP
            'take_profit': take_profit,
            'liquidation_price': calculate_liquidation_price(entry_price_filled, LEVERAGE, side),
            'timestamp': time.time(),
        }
        
        # グローバルポジションリストに追加
        OPEN_POSITIONS.append(new_pos)
        
        logging.info(f"✅ {symbol} {side.upper()} order filled. Price: {format_price(entry_price_filled)}, Contracts: {filled_amount:.4f}, Notional: {format_usdt(filled_usdt)}")

        return {
            'symbol': symbol, 
            'status': 'ok', 
            'message': 'Trade executed and position added to management list',
            'entry_price': entry_price_filled,
            'filled_amount': filled_amount,
            'filled_usdt': filled_usdt,
            'stop_loss': stop_loss,
            'take_profit': take_profit,
        }

    except ccxt.ExchangeError as e:
        error_msg = str(e)
        
        # 特定のエラーコードを検出して、ログレベルを変更
        is_fatal = True
        if '10007' in error_msg: 
            # 例: symbol not support api 
            logging.warning(f"{symbol} - Exchange Error (Code 10007 / API Limit/Restriction). Skipping.")
            error_msg = 'Code 10007: API Limit/Restriction'
            is_fatal = False
        elif '30005' in error_msg:
            # 例: 流動性不足 
            logging.warning(f"{symbol} - Exchange Error (Code 30005 / Insufficient Liquidity). Skipping.")
            error_msg = 'Code 30005: Insufficient Liquidity'
            is_fatal = False
        elif '400' in error_msg:
             # 例: Min Notional Value (Patch 71で事前チェック済みだが、念のため)
            logging.warning(f"{symbol} - Exchange Error (Code 400 / Min Notional Value). Skipping.")
            error_msg = 'Code 400: Min Notional Value'
            is_fatal = False
        
        if is_fatal:
            logging.error(f"{symbol}: Trade Exchange Error (Fatal): {error_msg}")
        
        return {'symbol': symbol, 'status': 'error', 'error_message': error_msg}
        
    except ccxt.NetworkError as e:
        logging.error(f"{symbol}: Trade Network Error: {e}")
        return {'symbol': symbol, 'status': 'error', 'error_message': 'Network Error'}
    except Exception as e:
        logging.error(f"{symbol}: Trade Unexpected Error: {e}")
        return {'symbol': symbol, 'status': 'error', 'error_message': f'Unexpected Error: {e}'}

async def check_and_execute_exit_logic_async(position: Dict) -> Tuple[bool, Optional[Dict]]:
    """
    ポジションのSL/TPをチェックし、該当する場合は決済注文を実行する。
    """
    global EXCHANGE_CLIENT, OPEN_POSITIONS
    
    symbol = position['symbol']
    side = position['side']
    stop_loss = position['stop_loss']
    take_profit = position['take_profit']
    contracts = position['contracts']
    
    if not EXCHANGE_CLIENT or not contracts > 0:
        return False, None
    
    try:
        # 1. 現在価格の取得 (非同期)
        ticker = await EXCHANGE_CLIENT.fetch_ticker(symbol)
        current_price = ticker['last']
        
        # 2. 決済条件のチェック
        exit_type = None
        
        if side == 'long':
            # ロングの場合: SLは価格が下落、TPは価格が上昇
            if current_price <= stop_loss:
                exit_type = 'SL'
            elif current_price >= take_profit:
                exit_type = 'TP'
        elif side == 'short':
            # ショートの場合: SLは価格が上昇、TPは価格が下落
            if current_price >= stop_loss:
                exit_type = 'SL'
            elif current_price <= take_profit:
                exit_type = 'TP'
                
        if exit_type is None:
            # 決済条件未達
            return False, None

        # 3. 決済注文の実行 (成行注文)
        order_side = 'sell' if side == 'long' else 'buy'
        
        logging.info(f"🚨 {symbol}: Exit signal detected ({exit_type}). Executing {order_side.upper()} market order.")
        
        # 注文
        order = await EXCHANGE_CLIENT.create_market_order(
            symbol=symbol, 
            side=order_side, 
            amount=contracts, 
        )
        
        # 注文結果の確認
        if order['status'] != 'closed' and order['status'] != 'filled':
            logging.warning(f"{symbol}: Exit order status is not filled: {order['status']}. Manual check required.")
            
        # 4. 決済結果の計算
        exit_price = order.get('average', current_price)
        
        # ポジションを管理リストから削除
        OPEN_POSITIONS = [p for p in OPEN_POSITIONS if p['symbol'] != symbol]
        
        # 損益の計算
        entry_price = position['entry_price']
        
        if side == 'long':
            profit_usdt = (exit_price - entry_price) * contracts
        else: # short
            profit_usdt = (entry_price - exit_price) * contracts
        
        # 損益率 (名目価値に対する比率)
        filled_usdt = position['filled_usdt']
        profit_rate = profit_usdt / filled_usdt if filled_usdt else 0.0
        
        # 保有期間
        holding_period = str(timedelta(seconds=int(time.time() - position['timestamp'])))

        exit_result = {
            'symbol': symbol,
            'status': 'closed',
            'exit_price': exit_price,
            'profit_usdt': profit_usdt,
            'profit_rate': profit_rate,
            'exit_type': exit_type,
            'holding_period': holding_period
        }
        
        logging.info(f"✅ {symbol} position closed by {exit_type}. Profit: {format_usdt(profit_usdt)} USDT ({profit_rate*100:.2f} %)")
        
        return True, exit_result
        
    except ccxt.NetworkError as e:
        logging.error(f"{symbol}: Exit Network Error: {e}")
    except ccxt.ExchangeError as e:
        logging.error(f"{symbol}: Exit Exchange Error: {e}")
    except Exception as e:
        logging.error(f"{symbol}: Exit Unexpected Error: {e}")
        
    return False, None

# ====================================================================================
# MAIN LOOPS
# ====================================================================================

async def position_monitoring_task():
    """
    ポジション監視ループ。10秒ごとに実行され、SL/TPトリガーをチェックする。
    """
    global MONITOR_INTERVAL, OPEN_POSITIONS
    logging.info("Starting position monitoring task...")
    
    while True:
        await asyncio.sleep(MONITOR_INTERVAL)
        
        if not OPEN_POSITIONS:
            continue
            
        logging.debug(f"Monitoring {len(OPEN_POSITIONS)} open positions...")
        
        # ポジションチェックタスクを並行して実行
        monitoring_tasks = [
            check_and_execute_exit_logic_async(position) 
            for position in OPEN_POSITIONS 
        ]
        
        # 並行実行
        results = await asyncio.gather(*monitoring_tasks)
        
        for is_closed, exit_result in results:
            if is_closed and exit_result:
                # 決済が実行された場合、Telegramに通知
                # 決済されたポジションの元のシグナル情報を検索 (厳密には不要だが、通知フォーマットを維持するため)
                closed_signal = next((s for s in LAST_ANALYSIS_SIGNALS if s['symbol'] == exit_result['symbol']), {})
                
                # SL/TPの値はポジションオブジェクト (position) のものを使用
                if not closed_signal:
                    # シグナルが見つからない場合、ポジションの情報から最低限を構築
                    closed_signal = {
                        'symbol': exit_result['symbol'],
                        'timeframe': 'N/A',
                        'score': 0.0,
                        'side': exit_result.get('side', 'N/A'),
                        'entry_price': exit_result.get('entry_price', 0.0),
                        'stop_loss': 0.0, # 決済後は不要
                        'take_profit': 0.0, # 決済後は不要
                        'liquidation_price': 0.0,
                        'rr_ratio': 0.0
                    }
                
                # ポジション決済通知を送信
                notification_message = format_telegram_message(
                    signal=closed_signal, 
                    context="ポジション決済", 
                    current_threshold=get_current_threshold(GLOBAL_MACRO_CONTEXT),
                    trade_result=exit_result,
                    exit_type=exit_result['exit_type']
                )
                await telegram_send_message(notification_message)
                
        # OPEN_POSITIONSはcheck_and_execute_exit_logic_async内で更新されている


# 💡 BOTのメイン実行ロジック
# 【エラー修正箇所: def -> async def に変更】
async def main_bot_loop():
    global LAST_SUCCESS_TIME, LAST_ANALYSIS_ONLY_NOTIFICATION_TIME
    global LAST_WEBSHARE_UPLOAD_TIME, LAST_ANALYSIS_SIGNALS
    global IS_FIRST_MAIN_LOOP_COMPLETED, GLOBAL_MACRO_CONTEXT
    
    logging.info("Main bot loop started.")
    
    while True:
        current_time = time.time()
        
        try:
            # 1. クライアントの初期化 (初回のみ)
            if not IS_CLIENT_READY:
                logging.info("Initializing exchange client...")
                account_status = await initialize_exchange()
                
                if account_status.get('error'):
                    logging.critical(f"Client initialization failed. Retrying in {LOOP_INTERVAL}s.")
                    await asyncio.sleep(LOOP_INTERVAL)
                    continue
                
            # 2. アカウント残高とマクロ情報の更新
            
            # 資産状況の更新 (取引停止中のチェックも含む)
            account_status = await fetch_account_status_async()
            if account_status.get('error'):
                logging.warning(f"Failed to fetch account status: {account_status['message']}. Retrying later.")
                
            # マクロコンテキストの更新
            GLOBAL_MACRO_CONTEXT = await get_macro_context_async()
            current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)

            
            # 3. 監視銘柄リストの更新 (SKIP_MARKET_UPDATEがFalseの場合)
            if not IS_FIRST_MAIN_LOOP_COMPLETED or (not SKIP_MARKET_UPDATE and current_time - LAST_SUCCESS_TIME > 60 * 30): # 30分ごと
                logging.info("Updating market symbols list...")
                updated_symbols, _ = await update_market_symbols_async()
                # 更新が失敗した場合はデフォルトリストを維持
                if updated_symbols and len(updated_symbols) > 0:
                     CURRENT_MONITOR_SYMBOLS = updated_symbols

            # 4. 取引シグナル生成
            logging.info(f"Analyzing {len(CURRENT_MONITOR_SYMBOLS)} symbols with threshold {current_threshold*100:.0f}...")
            
            # 全銘柄の分析を並行実行
            all_signals = await analyze_and_generate_signals(
                CURRENT_MONITOR_SYMBOLS, 
                GLOBAL_MACRO_CONTEXT
            )
            LAST_ANALYSIS_SIGNALS = all_signals # グローバル変数に保存

            # 5. シグナルフィルタリングと取引実行
            
            # 閾値とクールダウン期間でフィルタリング
            filtered_signals = []
            for signal in all_signals:
                symbol = signal['symbol']
                
                # 閾値チェック
                if signal['score'] < current_threshold:
                    continue
                    
                # クールダウンチェック (最後にシグナルが発生してから12時間経過しているか)
                if symbol in LAST_SIGNAL_TIME and (current_time - LAST_SIGNAL_TIME[symbol] < TRADE_SIGNAL_COOLDOWN):
                    logging.debug(f"{symbol}: Cooldown active. Skipping.")
                    continue
                
                # 既にポジションがあるかチェック
                if await check_position_status(symbol):
                    logging.debug(f"{symbol}: Position exists. Skipping.")
                    continue
                    
                # リスクベースサイジングのパラメータを計算し、シグナルに追加
                sizing_params = get_trade_sizing_and_params(
                    signal, 
                    signal['entry_price'], 
                    signal['stop_loss'],
                    min_amount_unit=0.001, # 暫定値。実際には取引所から取得すべき
                    price_precision=4      # 暫定値
                )
                
                if sizing_params.get('error'):
                    logging.warning(f"{symbol}: Sizing failed. Skipping trade.")
                    continue
                    
                signal['sizing_params'] = sizing_params
                signal['lot_size_units'] = sizing_params['lot_size_units']
                signal['notional_value'] = sizing_params['notional_value']
                signal['risk_usdt'] = sizing_params['risk_usdt']
                
                filtered_signals.append(signal)

            # TOP_SIGNAL_COUNT (通常1) のシグナルのみを取引試行
            signals_to_trade = filtered_signals[:TOP_SIGNAL_COUNT]
            
            if signals_to_trade:
                logging.info(f"Executing trade for {len(signals_to_trade)} signals...")
                
                tasks_to_process = [execute_trade_logic(signal) for signal in signals_to_trade]
                
                # 致命的なエラーの原因となった行: await は async def の内部でのみ使用可能
                # 修正により main_bot_loop が async となり、この行が正常に実行される
                results = await asyncio.gather(*tasks_to_process) # 内部で await 実行 
                
                # 取引結果の通知
                for signal, trade_result in zip(signals_to_trade, results):
                    
                    # 取引成功またはエラーで通知
                    if trade_result['status'] == 'ok' or trade_result['status'] == 'error':
                        
                        # エラーコード10007/30005/400の場合は通知をスキップ
                        error_msg = trade_result.get('error_message', '')
                        if '10007' in error_msg or '30005' in error_msg or '400' in error_msg:
                            logging.warning(f"{signal['symbol']}: Skipping notification for known error code.")
                            continue

                        # 成功またはその他のエラーで通知を送信
                        notification_message = format_telegram_message(
                            signal=signal, 
                            context="取引シグナル", 
                            current_threshold=current_threshold,
                            trade_result=trade_result
                        )
                        await telegram_send_message(notification_message)
                        
                        # 成功した場合のみ、クールダウン時間を記録
                        if trade_result['status'] == 'ok':
                            LAST_SIGNAL_TIME[signal['symbol']] = current_time

            else:
                logging.info(f"No trading signals found above threshold {current_threshold*100:.0f}.")
                
            # 6. 初回起動完了通知 (一度だけ実行)
            if not IS_FIRST_MAIN_LOOP_COMPLETED:
                startup_message = format_startup_message(
                    account_status=account_status,
                    macro_context=GLOBAL_MACRO_CONTEXT,
                    monitoring_count=len(CURRENT_MONITOR_SYMBOLS),
                    current_threshold=current_threshold
                )
                await telegram_send_message(startup_message)
                IS_FIRST_MAIN_LOOP_COMPLETED = True
            
            # 7. 定期分析専用通知 (1時間ごと)
            if current_time - LAST_ANALYSIS_ONLY_NOTIFICATION_TIME >= ANALYSIS_ONLY_INTERVAL:
                analysis_message = format_daily_analysis_message(
                    signals=all_signals,
                    current_threshold=current_threshold,
                    monitoring_count=len(CURRENT_MONITOR_SYMBOLS)
                )
                await telegram_send_message(analysis_message)
                LAST_ANALYSIS_ONLY_NOTIFICATION_TIME = current_time
                
            # 8. WebShareログアップロード (1時間ごと)
            if current_time - LAST_WEBSHARE_UPLOAD_TIME >= WEBSHARE_UPLOAD_INTERVAL:
                log_data = json.dumps({
                    'timestamp': datetime.now(JST).isoformat(),
                    'account_equity': ACCOUNT_EQUITY_USDT,
                    'open_positions': [
                        {
                            "symbol": p['symbol'],
                            "side": p['side'],
                            "entry_price": p['entry_price'],
                            "contracts": f"{p['contracts']:.4f}",
                            "notional_value_usdt": format_usdt(p['filled_usdt']),
                            "stop_loss": format_price(p['stop_loss']),
                            "take_profit": format_price(p['take_profit']),
                        } for p in OPEN_POSITIONS],
                    'last_signals': LAST_ANALYSIS_SIGNALS[:5],
                })
                await upload_log_to_webshare(log_data)
                LAST_WEBSHARE_UPLOAD_TIME = current_time


            # 成功時刻の更新
            LAST_SUCCESS_TIME = current_time
            
        except Exception as e:
            logging.error(f"Main bot loop unexpected error: {e}")
            
        # 次のループまで待機
        await asyncio.sleep(LOOP_INTERVAL)


# ====================================================================================
# FASTAPI INTEGRATION
# ====================================================================================

# 💡 ステータスAPIエンドポイント
@app.get("/status", summary="BOTの現在のステータスとポジション情報を取得")
async def get_bot_status():
    """BOTの現在の動作状況、アカウント残高、オープンポジションの一覧を返します。"""
    
    # 簡略化のため、グローバル変数から情報を取得
    current_time = datetime.now(JST).isoformat()
    current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
    
    return {
        "status": "Running" if IS_CLIENT_READY else "Initializing/Error",
        "version": "v20.0.25",
        "last_update_time_jst": current_time,
        "test_mode": TEST_MODE,
        "exchange": CCXT_CLIENT_NAME.upper(),
        "account_equity_usdt": format_usdt(ACCOUNT_EQUITY_USDT),
        "trading_threshold": f"{current_threshold*100:.0f}/100",
        "macro_context": GLOBAL_MACRO_CONTEXT,
        "monitoring_symbols_count": len(CURRENT_MONITOR_SYMBOLS),
        "open_positions_count": len(OPEN_POSITIONS),
        "open_positions": [
            {
                "symbol": p['symbol'],
                "side": p['side'],
                "entry_price": format_price(p['entry_price']),
                "contracts": f"{p['contracts']:.4f}",
                "notional_value_usdt": format_usdt(p['filled_usdt']),
                "stop_loss": format_price(p['stop_loss']),
                "take_profit": format_price(p['take_profit']),
            } for p in OPEN_POSITIONS
        ]
    }

# 💡 BOTのメインタスクをバックグラウンドで実行
@app.on_event("startup")
async def startup_event():
    """FastAPI起動時にBOTのメインタスクを実行する。"""
    logging.info("🌟 FastAPIアプリケーションが起動しました。BOTロジックを開始します...")
    # asyncio.create_task でメインループをバックグラウンドで開始
    asyncio.create_task(main_bot_loop())
    # ポジション監視タスクをバックグラウンドで開始
    asyncio.create_task(position_monitoring_task())


if __name__ == "__main__":
    # 💡 開発環境での実行を想定し、uvicornを設定
    # ポート番号は任意に設定可能
    port = int(os.getenv("API_PORT", 8000))
    logging.info(f"🌐 BOT APIサーバーを http://0.0.0.0:{port} で起動します。")
    
    # uvicorn.run の代わりに、asyncio.run と uvicorn.Server を使用し、
    # BOTロジックとWebサーバーを統合
    
    # uvicorn.run(app, host="0.0.0.0", port=port) # 以前のシンプルな実行方法
    
    # 開発環境で実行する場合の推奨 (FastAPIとBOTを統合)
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info")
    server = uvicorn.Server(config)
    
    # asyncio.run(server.serve()) # uvicornのサーバを直接実行 (Renderではメインプロセスがこれをブロックする)
    
    # Renderの実行環境に合わせるため、ここでは uvicorn main_render:app ... を前提とし、
    # __name__ == "__main__" ブロックは開発環境でのテスト用としてのみ使用する。
    # 実際のRenderデプロイでは、render.yaml または Web Service の Run Command で
    # uvicorn main_render:app --host 0.0.0.0 --port $PORT が実行される。
    
    try:
        # メインループを起動するタスクを登録し、サーバーを実行
        asyncio.run(server.serve())
    except KeyboardInterrupt:
        logging.info("Server shutdown.")
    except Exception as e:
        logging.error(f"An error occurred during server execution: {e}")
