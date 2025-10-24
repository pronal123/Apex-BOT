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
            trade_status_line = f"❌ **自動売買 失敗**: {trade_result.get('error_message', 'APIエラー')}"
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
                f"  - **注文タイプ**: <code>先物 (Future) / {order_type_text} ({side.capitalize()})</code>\n" 
                f"  - **レバレッジ**: <code>{LEVERAGE}</code> 倍\n" 
                f"  - **リスク許容額**: <code>{format_usdt(risk_usdt)}</code> USDT ({MAX_RISK_PER_TRADE_PERCENT*100:.2f}%)\n" 
                f"  - **約定数量**: <code>{filled_amount:.4f}</code> {symbol.split('/')[0]}\n"
                f"  - **名目約定額**: <code>{format_usdt(filled_usdt_notional)}</code> USDT\n" 
            )
            
    elif context == "ポジション決済":
        exit_type_final = trade_result.get('exit_type', exit_type or '不明')
        side_text = "ロング" if side == 'long' else "ショート"
        trade_status_line = f"🔴 **{side_text} ポジション決済**: {exit_type_final} トリガー ({LEVERAGE}x)" 
        
        entry_price = trade_result.get('entry_price', 0.0)
        exit_price = trade_result.get('exit_price', 0.0)
        pnl_usdt = trade_result.get('pnl_usdt', 0.0)
        pnl_rate = trade_result.get('pnl_rate', 0.0)
        
        filled_amount_raw = trade_result.get('filled_amount', 0.0)
        # trade_resultから取得した値が文字列（取引所APIからの応答でよくある形式）の場合にfloatに変換する
        try:
            filled_amount = float(filled_amount_raw)
        except (ValueError, TypeError):
            filled_amount = 0.0
        
        pnl_sign = "✅ 利益確定" if pnl_usdt >= 0 else "❌ 損切り"
        
        trade_section = (
            f"💰 **決済実行結果** - {pnl_sign}\n"
            f"  - **エントリー価格**: <code>{format_price(entry_price)}</code>\n"
            f"  - **決済価格**: <code>{format_price(exit_price)}</code>\n"
            f"  - **約定数量**: <code>{filled_amount:.4f}</code> {symbol.split('/')[0]}\n"
            f"  - **純損益**: <code>{'+' if pnl_usdt >= 0 else ''}{format_usdt(pnl_usdt)}</code> USDT ({pnl_rate*100:.2f}%)\n" 
        )
            
    
    message = (
        f"🚀 **Apex TRADE {context}** ({side.capitalize()})\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - **日時**: {now_jst} (JST)\n"
        f"  - **銘柄**: <b>{symbol}</b> ({timeframe})\n"
        f"  - **ステータス**: {trade_status_line}\n" 
        f"  - **総合スコア**: <code>{score * 100:.2f} / 100</code>\n"
        f"  - **取引閾値**: <code>{current_threshold * 100:.2f}</code> 点\n"
        f"  - **推定勝率**: <code>{estimated_wr}</code>\n"
        f"  - **リスクリワード比率 (RRR)**: <code>1:{rr_ratio:.2f}</code>\n"
        f"  - **エントリー**: <code>{format_price(entry_price)}</code>\n"
        f"  - **ストップロス (SL)**: <code>{format_price(stop_loss)}</code>\n"
        f"  - **テイクプロフィット (TP)**: <code>{format_price(take_profit)}</code>\n"
        f"  - **清算価格 (Liq. Price)**: <code>{format_price(liquidation_price)}</code>\n" 
        f"  - **リスク幅 (SL)**: <code>{format_usdt(risk_width)}</code> USDT\n"
        f"  - **リワード幅 (TP)**: <code>{format_usdt(reward_width)}</code> USDT\n"
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
        
    message += (f"<i>Bot Ver: v20.0.25 - Future Trading / 10x Leverage (Patch 71: MEXC Min Notional Value FIX for Lot Size 400)</i>") # BOTバージョンを更新
    return message


async def send_telegram_notification(message: str) -> bool:
    """Telegramにメッセージを送信する (変更なし)"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.error("❌ Telegram設定 (TOKEN/ID) が不足しています。通知をスキップします。")
        return False
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        'chat_id': TELEGRAM_CHAT_ID,
        'text': message,
        'parse_mode': 'HTML'
    }
    
    try:
        response = await asyncio.to_thread(requests.post, url, data=payload, timeout=5)
        response.raise_for_status()
        logging.info(f"✅ Telegram通知を送信しました。")
        return True
    except requests.exceptions.HTTPError as e:
        error_details = response.json() if 'response' in locals() else 'N/A'
        logging.error(f"❌ Telegram HTTPエラー: {e} - 詳細: {error_details}")
    except requests.exceptions.RequestException as e:
        logging.error(f"❌ Telegramリクエストエラー: {e}")
    return False

def _to_json_compatible(obj):
    """
    再帰的にオブジェクトをJSON互換の型に変換するヘルパー関数。 (変更なし)
    """
    if isinstance(obj, dict):
        return {k: _to_json_compatible(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_to_json_compatible(elem) for elem in obj]
    elif isinstance(obj, (bool, np.bool_)):
        return str(obj) 
    elif isinstance(obj, np.generic):
        return obj.item()
    return obj


def log_signal(data: Dict, log_type: str, trade_result: Optional[Dict] = None) -> None:
    """シグナルまたは取引結果をローカルファイルにログする (変更なし)"""
    try:
        log_entry = {
            'timestamp_jst': datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
            'log_type': log_type,
            'symbol': data.get('symbol', 'N/A'),
            'side': data.get('side', 'N/A'), # ショート対応
            'timeframe': data.get('timeframe', 'N/A'),
            'score': data.get('score', 0.0),
            'rr_ratio': data.get('rr_ratio', 0.0),
            'trade_result': trade_result or data.get('trade_result', None),
            'full_data': data,
        }
        
        cleaned_log_entry = _to_json_compatible(log_entry)

        log_file = f"apex_bot_{log_type.lower().replace(' ', '_')}_log.jsonl"
        with open(log_file, 'a', encoding='utf-8') as f:
            f.write(json.dumps(cleaned_log_entry, ensure_ascii=False) + '\n')
            
        logging.info(f"✅ {log_type}ログをファイルに記録しました。")
    except Exception as e:
        logging.error(f"❌ ログ書き込みエラー: {e}", exc_info=True)


# ====================================================================================
# WEBSHARE FUNCTION (HTTP POST) 
# ====================================================================================

async def send_webshare_update(data: Dict[str, Any]):
    """取引データをHTTP POSTで外部サーバーに送信する"""
    
    if WEBSHARE_METHOD == "HTTP":
        if not WEBSHARE_POST_URL or "your-webshare-endpoint.com/upload" in WEBSHARE_POST_URL:
            logging.warning("⚠️ WEBSHARE_POST_URLが設定されていません。またはデフォルト値のままです。送信をスキップします。")
            return

        try:
            cleaned_data = _to_json_compatible(data)
            
            response = await asyncio.to_thread(requests.post, WEBSHARE_POST_URL, json=cleaned_data, timeout=10)
            response.raise_for_status()

            logging.info(f"✅ WebShareデータ (HTTP POST) を送信しました。ステータス: {response.status_code}")

        except requests.exceptions.RequestException as e:
            logging.error(f"❌ WebShare (HTTP POST) エラー: {e}")
            await send_telegram_notification(f"🚨 <b>WebShareエラー (HTTP POST)</b>\nデータ送信に失敗しました: <code>{e}</code>")

    else:
        logging.warning("⚠️ WEBSHARE_METHOD が 'HTTP' 以外に設定されています。WebShare送信をスキップします。")
        

# ====================================================================================
# CCXT & DATA ACQUISITION
# ====================================================================================

async def initialize_exchange_client() -> bool:
    """CCXTクライアントを初期化し、市場情報をロードする (Patch 68で LEVERAGE_SETTING_DELAY を使用)"""
    global EXCHANGE_CLIENT, IS_CLIENT_READY
    
    IS_CLIENT_READY = False
    
    if not API_KEY or not SECRET_KEY:
         logging.critical("❌ CCXT初期化スキップ: APIキー または SECRET_KEY が設定されていません。")
         return False
         
    # 💡 既存のクライアントがあれば、リソースを解放する
    if EXCHANGE_CLIENT:
        try:
            # 既存のクライアントを正常にクローズ
            await EXCHANGE_CLIENT.close()
            logging.info("✅ 既存のCCXTクライアントセッションを正常にクローズしました。")
        except Exception as e:
            # 競合状態や既にクローズされている場合にエラーになることがあるが、無視して続行
            logging.warning(f"⚠️ 既存クライアントのクローズ中にエラーが発生しましたが続行します: {e}")
        EXCHANGE_CLIENT = None
         
    try:
        client_name = CCXT_CLIENT_NAME.lower()
        if client_name == 'binance':
            exchange_class = ccxt_async.binance
        elif client_name == 'bybit':
            exchange_class = ccxt_async.bybit
        elif client_name == 'mexc':
            exchange_class = ccxt_async.mexc
        else:
            logging.error(f"❌ 未対応の取引所クライアント: {CCXT_CLIENT_NAME}")
            return False

        options = {
            'defaultType': 'future', 
        }

        # 💡 ネットワークタイムアウトを延長 (例: 30秒 = 30000ms)
        timeout_ms = 30000 
        
        EXCHANGE_CLIENT = exchange_class({
            'apiKey': API_KEY,
            'secret': SECRET_KEY,
            'enableRateLimit': True,
            'options': options,
            'timeout': timeout_ms # ★ タイムアウト設定を追加
        })
        logging.info(f"✅ CCXTクライアントの初期化設定完了。リクエストタイムアウト: {timeout_ms/1000}秒。") 
        
        await EXCHANGE_CLIENT.load_markets() 
        
        # レバレッジの設定 (MEXC向け)
        if EXCHANGE_CLIENT.id == 'mexc':
            symbols_to_set_leverage = []
            # 💡 NOTE: load_marketsで全ての市場をロードしたため、ここでFuture/Swap市場を探してレバレッジを設定する
            for mkt in EXCHANGE_CLIENT.markets.values():
                 # USDT建てのSwap/Future市場を探す
                 # symbol: 'BTC/USDT:USDT' の形式を想定
                 if mkt['quote'] == 'USDT' and mkt['type'] in ['swap', 'future'] and mkt['active']:
                     # ユーザーが指定するCCXT標準のシンボル形式
                     symbols_to_set_leverage.append(mkt['symbol']) 
            
            # --- 🚀 Patch 68 FIX: set_leverage に LEVERAGE_SETTING_DELAY を適用 ---

            # set_leverage() が openType と positionType の両方を要求するため、両方の設定を行います。
            for symbol in symbols_to_set_leverage:
                
                # openType: 2 は Cross Margin
                # positionType: 1 は Long (買い) ポジション用
                try:
                    # パラメータ: openType=2 (Cross Margin), positionType=1 (Long)
                    await EXCHANGE_CLIENT.set_leverage(
                        LEVERAGE, 
                        symbol, 
                        params={'openType': 2, 'positionType': 1}
                    )
                    logging.info(f"✅ {symbol} のレバレッジを {LEVERAGE}x (Cross Margin / Long) に設定しました。")
                except Exception as e:
                    logging.warning(f"⚠️ {symbol} のレバレッジ/マージンモード設定 (Long) に失敗しました: {e}")
                
                # 💥 レートリミット対策として遅延を挿入 (重要: 0.5s -> 1.5s)
                await asyncio.sleep(LEVERAGE_SETTING_DELAY)

                # positionType: 2 は Short (売り) ポジション用
                try:
                    # パラメータ: openType=2 (Cross Margin), positionType=2 (Short)
                    await EXCHANGE_CLIENT.set_leverage(
                        LEVERAGE, 
                        symbol, 
                        params={'openType': 2, 'positionType': 2}
                    )
                    logging.info(f"✅ {symbol} のレバレッジを {LEVERAGE}x (Cross Margin / Short) に設定しました。")
                except Exception as e:
                    logging.warning(f"⚠️ {symbol} のレバレッジ/マージンモード設定 (Short) に失敗しました: {e}")

                # 💥 レートリミット対策として遅延を挿入 (重要: 0.5s -> 1.5s)
                await asyncio.sleep(LEVERAGE_SETTING_DELAY)

            logging.info(f"✅ MEXCの主要な先物銘柄 ({len(symbols_to_set_leverage)}件) に対し、レバレッジを {LEVERAGE}x、マージンモードを 'cross' に設定しました。")
            
            # --- 🚀 Patch 68 FIX 終了 ---

        # ログメッセージを 'future' モードに変更
        logging.info(f"✅ CCXTクライアント ({CCXT_CLIENT_NAME}) を先物取引モードで初期化し、市場情報をロードしました。")
        IS_CLIENT_READY = True
        return True

    except ccxt.AuthenticationError as e:
        logging.critical(f"❌ CCXT初期化失敗 - 認証エラー: APIキー/シークレットを確認してください。{e}", exc_info=True)
    except ccxt.ExchangeNotAvailable as e:
        logging.critical(f"❌ CCXT初期化失敗 - 取引所接続エラー: サーバーが利用できません。{e}", exc_info=True)
    except ccxt.NetworkError as e:
        # RequestTimeoutもccxt.NetworkErrorを継承しているため、ここで捕捉
        logging.critical(f"❌ CCXT初期化失敗 - ネットワークエラー/タイムアウト: 接続を確認してください。{e}", exc_info=True)
    except Exception as e:
        # Patch 57で RuntimeError: Session is closed の原因となる競合状態を避けるため、このクリティカルログは維持
        logging.critical(f"❌ CCXTクライアント初期化失敗 - 予期せぬエラー: {e}", exc_info=True)
    
    EXCHANGE_CLIENT = None
    return False

async def fetch_account_status() -> Dict:
    """CCXTから先物口座の残高と利用可能マージン情報を取得し、グローバル変数に格納する。 (変更なし)"""
    global EXCHANGE_CLIENT, ACCOUNT_EQUITY_USDT

    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.error("❌ 口座ステータス取得失敗: CCXTクライアントが準備できていません。")
        return {'total_usdt_balance': 0.0, 'open_positions': [], 'error': True}

    try:
        # 💡 先物/マージン口座の情報を取得
        balance_info = await EXCHANGE_CLIENT.fetch_balance({'type': 'future'})
        
        # 'total' は全資産、'free' は利用可能な資産
        total_balance_usdt = balance_info.get('total', {}).get('USDT', 0.0)
        
        # 💡 ポジション情報を取得 (先物取引の場合)
        positions_raw = await EXCHANGE_CLIENT.fetch_positions()
        
        # 現在のポジションのみをフィルタリング
        open_positions = []
        for p in positions_raw:
            # 'contract' または 'notional' がゼロでないポジションをオープンポジションと見なす
            if p.get('contracts', 0) != 0 or p.get('notional', 0) != 0:
                # ポジションサイドを ccxt の side 'long' or 'short' から取得
                side = p.get('side', 'N/A').lower() 
                
                # SL/TPはボットがローカルで管理するため、ここでは ccxt から取得できる基本情報のみ格納
                open_positions.append({
                    'symbol': p['symbol'],
                    'side': side,
                    'entry_price': p.get('entryPrice', 0.0),
                    'contracts': p.get('contracts', 0.0),
                    # MEXC, Bybitは 'notional' (名目価値)がfilled_usdtに相当
                    'filled_usdt': p.get('notional', 0.0), 
                    'liquidation_price': p.get('liquidationPrice', 0.0),
                })
        
        # グローバル変数を更新
        ACCOUNT_EQUITY_USDT = total_balance_usdt
        # OPEN_POSITIONS の更新は、メインループのposition_monitoring_taskで行う (SL/TP情報を含めるため)
        # ここでは ccxt から取得した生のポジション情報を返します
        
        logging.info(f"✅ 口座ステータス取得成功: 総資産 {format_usdt(total_balance_usdt)} USDT, オープンポジション {len(open_positions)} 件。")

        return {
            'total_usdt_balance': total_balance_usdt,
            'open_positions': open_positions,
            'error': False
        }

    except ccxt.ExchangeError as e:
        if 'not found' in str(e) or 'does not exist' in str(e):
             logging.error(f"❌ 口座ステータス取得失敗: 先物口座情報が見つかりません。設定を確認してください。{e}")
             await send_telegram_notification(f"🚨 **致命的なエラー**\nCCXTクライアントで先物口座情報の取得に失敗しました: {e}。取引を一時停止します。")
        else:
            logging.error(f"❌ 口座ステータス取得失敗: CCXTエラー {e}")
        return {'total_usdt_balance': 0.0, 'open_positions': [], 'error': True}
    except ccxt.NetworkError as e:
        logging.error(f"❌ 口座ステータス取得失敗: ネットワークエラー {e}")
        return {'total_usdt_balance': 0.0, 'open_positions': [], 'error': True}
    except Exception as e:
        logging.critical(f"❌ 口座ステータス取得失敗 - 予期せぬエラー: {e}", exc_info=True)
        return {'total_usdt_balance': 0.0, 'open_positions': [], 'error': True}


# ====================================================================================
# MACRO & SENTIMENT ANALYSIS
# ====================================================================================

# --- 修正開始: calculate_fgi関数の追加 (または既存の関数の置き換え) ---
async def calculate_fgi():
    """ 
    Fear & Greed Index (FGI)を取得し、マクロコンテキストを更新する。
    ログに記録されていた「FGI APIから不正なデータを受信」エラーに対応するため、
    パース処理に厳密なチェックとエラーハンドリングを追加。
    """
    global GLOBAL_MACRO_CONTEXT
    try:
        logging.info("ℹ️ FGI API (Alternative.me) からデータを取得しています...")
        
        # requests.getを非同期で実行
        response = await asyncio.to_thread(requests.get, FGI_API_URL, timeout=10)
        response.raise_for_status() # HTTPエラー (4xx, 5xx) を発生させる (e.g. 404, 500)
        
        data = response.json()
        
        # 【修正】不正なデータ構造に対応するための厳密なパースロジック
        if 'data' not in data or not isinstance(data['data'], list) or not data['data']:
             raise ValueError("FGI API returned invalid data structure (missing 'data' list or empty).")
        
        fgi_entry = data['data'][0]
        raw_value = fgi_entry.get('value')

        if raw_value is None:
             raise ValueError("FGI API returned data without a 'value' field.")

        fgi_value = float(raw_value)
        fgi_raw_classification = fgi_entry.get('value_classification', 'N/A')

        # FGI値 (0-100) を -1.0 から +1.0 の範囲に正規化する (50が0.0)
        fgi_proxy = (fgi_value - 50) / 50 
        
        # proxyにFGI_PROXY_BONUS_MAXを乗算し、マクロ影響スコアとして使用
        fgi_macro_effect = fgi_proxy * FGI_PROXY_BONUS_MAX
        
        # マクロコンテキストを更新
        GLOBAL_MACRO_CONTEXT['fgi_proxy'] = fgi_macro_effect 
        GLOBAL_MACRO_CONTEXT['fgi_raw_value'] = f"{fgi_value:.0f} ({fgi_raw_classification})"
        
        # 💡 Forexボーナスは機能削除されたため、0.0で固定
        GLOBAL_MACRO_CONTEXT['forex_bonus'] = 0.0

        logging.info(f"✅ FGI更新: {GLOBAL_MACRO_CONTEXT['fgi_raw_value']} (Proxy Score: {fgi_macro_effect*100:.2f})")
        
    except requests.exceptions.RequestException as e:
        logging.error(f"❌ FGI APIリクエストエラー: {e}")
    except ValueError as e:
        # FGI APIから不正なデータを受信した場合
        logging.error(f"❌ FGI APIから不正なデータを受信: {e}. FGIを0.0に設定します。")
        GLOBAL_MACRO_CONTEXT['fgi_proxy'] = 0.0
        GLOBAL_MACRO_CONTEXT['fgi_raw_value'] = 'Error'
    except Exception as e:
        logging.error(f"❌ FGI計算中に予期せぬエラー: {e}")
        GLOBAL_MACRO_CONTEXT['fgi_proxy'] = 0.0
        GLOBAL_MACRO_CONTEXT['fgi_raw_value'] = 'Error'
        
    # --- 修正終了 ---

async def get_top_volume_symbols() -> List[str]:
    """取引所APIから出来高トップの銘柄を取得し、監視対象リストを更新する。 (変更なし)"""
    global EXCHANGE_CLIENT, CURRENT_MONITOR_SYMBOLS
    
    if SKIP_MARKET_UPDATE:
        logging.warning("⚠️ SKIP_MARKET_UPDATEが有効なため、監視銘柄の更新をスキップします。")
        return CURRENT_MONITOR_SYMBOLS
        
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.error("❌ 出来高情報取得失敗: CCXTクライアントが準備できていません。デフォルトリストを使用します。")
        return DEFAULT_SYMBOLS

    try:
        # 💡 先物取引所の場合、先物/スワップ市場のティックを取得
        futures_markets = [
            m['symbol'] for m in EXCHANGE_CLIENT.markets.values() 
            if m['quote'] == 'USDT' and m['type'] in ['swap', 'future'] and m['active']
        ]
        
        tickers = await EXCHANGE_CLIENT.fetch_tickers(futures_markets)
        
        # 24時間出来高 (quote volume) を基準にソート
        # 'quoteVolume' (USDT建での取引量) を使用することが一般的
        volume_data = []
        for symbol, ticker in tickers.items():
            # 'quoteVolume' または 'baseVolume' * 'last' のうち、利用可能なものを優先
            volume = ticker.get('quoteVolume') 
            if volume is None:
                # 出来高情報がない場合は除外
                continue
                
            volume_data.append({
                'symbol': symbol,
                'quote_volume': volume
            })
            
        # 出来高で降順にソートし、TOP_SYMBOL_LIMIT に制限
        sorted_symbols = sorted(volume_data, key=lambda x: x['quote_volume'], reverse=True)
        top_symbols = [d['symbol'] for d in sorted_symbols[:TOP_SYMBOL_LIMIT]]
        
        # デフォルトの基軸通貨（BTC, ETHなど）がトップリストに含まれていない場合は追加
        for default_sym in DEFAULT_SYMBOLS:
            if default_sym not in top_symbols:
                top_symbols.append(default_sym)

        logging.info(f"✅ 出来高情報に基づく監視銘柄リストを更新しました。合計: {len(top_symbols)} 銘柄。")
        CURRENT_MONITOR_SYMBOLS = top_symbols
        return top_symbols

    except ccxt.ExchangeError as e:
        logging.error(f"❌ 出来高情報取得失敗: CCXTエラー {e}。デフォルトリストを使用します。")
        return DEFAULT_SYMBOLS
    except ccxt.NetworkError as e:
        logging.error(f"❌ 出来高情報取得失敗: ネットワークエラー {e}。デフォルトリストを使用します。")
        return DEFAULT_SYMBOLS
    except Exception as e:
        logging.error(f"❌ 出来高情報取得中に予期せぬエラー: {e}。デフォルトリストを使用します。", exc_info=True)
        return DEFAULT_SYMBOLS

# ====================================================================================
# OHLCV DATA & TECHNICAL ANALYSIS
# ====================================================================================

async def get_historical_ohlcv(symbol: str, timeframe: str, limit: int = 1000) -> Optional[pd.DataFrame]:
    """
    CCXTからOHLCVデータを取得し、Pandas DataFrameとして返す。
    取得に失敗した場合、またはデータが不十分な場合はNoneを返す。
    """
    global EXCHANGE_CLIENT

    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.error(f"❌ OHLCVデータ取得失敗: CCXTクライアントが準備できていません。({symbol}, {timeframe})")
        return None

    try:
        # API呼び出し
        ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(symbol, timeframe, limit=limit)
        
        if not ohlcv or len(ohlcv) < limit:
            logging.warning(f"⚠️ {symbol} - {timeframe}: データが不十分です (取得数: {len(ohlcv)} < 要求数: {limit})。")
            return None

        # DataFrameに変換
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        
        # 💡 データ型を適切に設定
        df['open'] = pd.to_numeric(df['open'], errors='coerce')
        df['high'] = pd.to_numeric(df['high'], errors='coerce')
        df['low'] = pd.to_numeric(df['low'], errors='coerce')
        df['close'] = pd.to_numeric(df['close'], errors='coerce')
        df['volume'] = pd.to_numeric(df['volume'], errors='coerce')

        # タイムスタンプをJSTに変換しインデックスに設定
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True).dt.tz_convert(JST)
        df.set_index('timestamp', inplace=True)
        
        # 💡 【エラー修正/改良】NaN値を持つ行を削除し、下流のPandas-TA計算でのエラーを防ぐ
        original_len = len(df)
        df.dropna(inplace=True) 
        if len(df) < original_len:
            logging.warning(f"⚠️ {symbol} - {timeframe}: {original_len - len(df)}件の不完全なデータポイントを削除しました。")
            
        return df
        
    except ccxt.NetworkError as e:
        logging.error(f"❌ OHLCVデータ取得失敗 - ネットワークエラー: {e} ({symbol}, {timeframe})")
    except ccxt.ExchangeError as e:
        # 💡 Code 10007 / 30005 のようなエラーコードをCCXTの例外から抽出してログに出す
        if '10007' in str(e) or '30005' in str(e) or 'symbol not supported' in str(e):
            # 例: MEXCの Code 10007 (symbol not support api), Code 30005 (流動性不足)
            logging.warning(f"⚠️ {symbol} - {timeframe}: 取引所エラー (Code 10007/30005相当、またはサポート対象外) - スキップします。")
        else:
            logging.error(f"❌ OHLCVデータ取得失敗 - 取引所エラー: {e} ({symbol}, {timeframe})")
    except Exception as e:
        logging.error(f"❌ OHLCVデータ取得中に予期せぬエラー: {e} ({symbol}, {timeframe})", exc_info=True)
        
    return None

def calculate_technical_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Pandas-TAを使用して必要なテクニカル指標を計算し、DataFrameに追加する。 (変更なし)"""
    
    # 💡 必要なインディケータの計算
    df.ta.sma(length=LONG_TERM_SMA_LENGTH, append=True) # 長期SMA
    df.ta.rsi(length=14, append=True) # RSI
    df.ta.macd(append=True) # MACD
    df.ta.atr(length=ATR_LENGTH, append=True) # ATR (SL/TP計算用)
    df.ta.bbands(length=20, append=True) # ボリンジャーバンド
    df.ta.obv(append=True) # OBV

    # インディケータ名を取得し、NaN値を除去（dropnaはget_historical_ohlcvで実施済みだが、念のため）
    # ta計算で生じるNaNを考慮し、最も古いデータは削除する必要がある (約200期間)
    df.dropna(inplace=True) 
    
    # 💡 データが残っているか再チェック
    if df.empty:
        raise ValueError("テクニカル指標計算後、データが残っていません。")

    return df

def analyze_signal(symbol: str, timeframe: str, df: pd.DataFrame, current_price: float, macro_context: Dict) -> Optional[Dict]:
    """
    テクニカル指標に基づいて取引シグナルを分析し、スコアと取引計画を生成する。
    Long/Shortの双方で、厳密なリスクリワード比率 (RRR) とストップロス (SL) を計算する。
    """
    if df.empty:
        return None

    last = df.iloc[-1]
    
    # --- 1. スコアリングの初期化 ---
    long_score = BASE_SCORE
    short_score = BASE_SCORE
    tech_data = {}
    
    # --- 2. トレンド・構造分析 ---
    
    # 長期トレンド: SMA200との比較
    sma200 = last[f'SMA_{LONG_TERM_SMA_LENGTH}']
    
    long_term_penalty_value = 0.0
    if current_price < sma200:
        long_term_penalty_value = LONG_TERM_REVERSAL_PENALTY # ロング不利
    else:
        # 価格がSMA200の上にある場合、ショートは不利
        long_term_penalty_value = LONG_TERM_REVERSAL_PENALTY # ショート不利
        
    # スコア適用
    if current_price < sma200:
        long_score -= long_term_penalty_value
    else:
        short_score -= long_term_penalty_value
        
    tech_data['long_term_reversal_penalty_value'] = long_term_penalty_value
    
    # 構造的ピボットボーナス (SMA50/100の支持/抵抗を考慮)
    pivot_bonus = 0.0
    sma50 = df.ta.sma(length=50, append=False).iloc[-1]
    sma100 = df.ta.sma(length=100, append=False).iloc[-1]
    
    if current_price > sma200 and current_price > sma100 and current_price > sma50:
        # 全ての長期SMAが支持線として機能 (ロング優位)
        pivot_bonus = STRUCTURAL_PIVOT_BONUS
    elif current_price < sma200 and current_price < sma100 and current_price < sma50:
        # 全ての長期SMAが抵抗線として機能 (ショート優位)
        pivot_bonus = STRUCTURAL_PIVOT_BONUS
        
    long_score += pivot_bonus
    short_score += pivot_bonus
    tech_data['structural_pivot_bonus'] = pivot_bonus
    
    # --- 3. モメンタム分析 ---
    
    # RSIモメンタム
    rsi = last[f'RSI_14']
    
    # MACDクロスによるペナルティ
    macd_hist = last['MACDh_12_26_9']
    macd_penalty_value = 0.0
    
    if macd_hist < 0:
        # MACDがゼロライン以下の場合 (ショートモメンタム)、ロングにペナルティ
        macd_penalty_value = MACD_CROSS_PENALTY 
        long_score -= macd_penalty_value
    
    if macd_hist > 0:
        # MACDがゼロライン以上の場合 (ロングモメンタム)、ショートにペナルティ
        macd_penalty_value = MACD_CROSS_PENALTY
        short_score -= macd_penalty_value
        
    tech_data['macd_penalty_value'] = macd_penalty_value # 記録用

    # OBVモメンタム (OBVの方向と価格の方向が一致しているか)
    obv_bonus_value = 0.0
    
    # OBVのSMA50 (中間トレンド) を使用
    obv_sma = df['OBV'].rolling(window=50).mean()
    last_obv_sma = obv_sma.iloc[-1]
    
    if last['OBV'] > last_obv_sma and current_price > df.iloc[-2]['close']:
        # OBVが上向き、価格も上昇 -> ロングにボーナス
        obv_bonus_value = OBV_MOMENTUM_BONUS
        long_score += obv_bonus_value
    elif last['OBV'] < last_obv_sma and current_price < df.iloc[-2]['close']:
        # OBVが下向き、価格も下落 -> ショートにボーナス
        obv_bonus_value = OBV_MOMENTUM_BONUS
        short_score += obv_bonus_value
        
    tech_data['obv_momentum_bonus_value'] = obv_bonus_value
    
    # --- 4. 流動性・ボラティリティ・マクロ分析 ---
    
    # 流動性ボーナス: 出来高 (直近20期間のSMAとの比較)
    volume_sma = df['volume'].rolling(window=20).mean()
    liquidity_bonus_value = 0.0
    if last['volume'] > volume_sma.iloc[-1] * 1.5:
        # 直近の出来高がSMAの1.5倍以上
        liquidity_bonus_value = LIQUIDITY_BONUS_MAX
        long_score += liquidity_bonus_value
        short_score += liquidity_bonus_value
        
    tech_data['liquidity_bonus_value'] = liquidity_bonus_value
    
    # ボラティリティペナルティ: ボリンジャーバンドの外側で終値が付いた場合
    bb_upper = last['BBU_20_2.0']
    bb_lower = last['BBL_20_2.0']
    
    volatility_penalty = 0.0
    if current_price > bb_upper or current_price < bb_lower:
        # 価格がBBの外側にいる = 過熱状態
        volatility_penalty = -0.10 # 大きなペナルティ
        long_score += volatility_penalty # どちらのサイドにもペナルティ
        short_score += volatility_penalty
        
    tech_data['volatility_penalty_value'] = volatility_penalty
    
    # マクロ要因ボーナス (FGI)
    fgi_proxy_score = macro_context.get('fgi_proxy', 0.0)
    
    # FGI > 0 (Greed/リスクオン) の場合、ロングにボーナス、ショートにペナルティ
    if fgi_proxy_score > 0:
        long_score += abs(fgi_proxy_score) 
        short_score -= abs(fgi_proxy_score) * 0.5 # ショートペナルティは半減
    # FGI < 0 (Fear/リスクオフ) の場合、ショートにボーナス、ロングにペナルティ
    elif fgi_proxy_score < 0:
        short_score += abs(fgi_proxy_score) 
        long_score -= abs(fgi_proxy_score) * 0.5 # ロングペナルティは半減
        
    tech_data['sentiment_fgi_proxy_bonus'] = fgi_proxy_score
    tech_data['forex_bonus'] = macro_context.get('forex_bonus', 0.0) # 0.0で固定されるが互換性のために残す

    # --- 5. 最適なシグナルを選択し、SL/TPを計算 ---
    
    # 最終スコアを決定
    final_long_score = round(max(0.0, long_score), 4)
    final_short_score = round(max(0.0, short_score), 4)
    
    best_side = 'none'
    best_score = 0.0
    
    # ロングが優位
    if final_long_score > final_short_score and final_long_score >= get_current_threshold(macro_context):
        best_side = 'long'
        best_score = final_long_score
    # ショートが優位
    elif final_short_score > final_long_score and final_short_score >= get_current_threshold(macro_context):
        best_side = 'short'
        best_score = final_short_score
    # どちらも閾値を超えない
    else:
        return None 
        
    # ATRに基づく動的SL/TPの計算
    atr_value = last[f'ATR_{ATR_LENGTH}']
    
    # 💡 リスクリワード比率 (RRR) の動的調整 (スコアが高いほどRRRを改善)
    # 閾値 0.75で RRR = 1:1.5
    # 閾値 0.90で RRR = 1:2.5
    if best_score >= 0.90:
        target_rr_ratio = 2.5
    elif best_score >= 0.85:
        target_rr_ratio = 2.0
    elif best_score >= 0.75:
        target_rr_ratio = 1.5
    elif best_score >= get_current_threshold(macro_context):
        # 閾値ギリギリの場合は最低限のRRR
        target_rr_ratio = 1.25 
    else:
        # この分岐には来ないはず
        return None 
        
    # SLの絶対価格をATRに基づいて計算
    if best_side == 'long':
        # SL = エントリー価格 - (ATR * 乗数)
        sl_diff = atr_value * ATR_MULTIPLIER_SL
        stop_loss = current_price - sl_diff
        
        # SLの最小パーセンテージリスクを保証
        min_sl_diff = current_price * MIN_RISK_PERCENT
        if sl_diff < min_sl_diff:
            stop_loss = current_price - min_sl_diff
            sl_diff = min_sl_diff # リスク幅を更新
            
        # TP = エントリー価格 + (SL幅 * RRR)
        take_profit = current_price + (sl_diff * target_rr_ratio)
        
    elif best_side == 'short':
        # SL = エントリー価格 + (ATR * 乗数)
        sl_diff = atr_value * ATR_MULTIPLIER_SL
        stop_loss = current_price + sl_diff

        # SLの最小パーセンテージリスクを保証
        min_sl_diff = current_price * MIN_RISK_PERCENT
        if sl_diff < min_sl_diff:
            stop_loss = current_price + min_sl_diff
            sl_diff = min_sl_diff # リスク幅を更新
            
        # TP = エントリー価格 - (SL幅 * RRR)
        take_profit = current_price - (sl_diff * target_rr_ratio)
        
    # 清算価格の計算 (グローバルなレバレッジと維持証拠金率を使用)
    liquidation_price = calculate_liquidation_price(
        entry_price=current_price, 
        leverage=LEVERAGE, 
        side=best_side, 
        maintenance_margin_rate=MIN_MAINTENANCE_MARGIN_RATE
    )

    # 構造化されたシグナルを返す
    signal_data = {
        'symbol': symbol,
        'timeframe': timeframe,
        'score': best_score,
        'side': best_side,
        'entry_price': current_price,
        'stop_loss': stop_loss,
        'take_profit': take_profit,
        'liquidation_price': liquidation_price,
        'rr_ratio': target_rr_ratio,
        'tech_data': tech_data,
        'trade_time': datetime.now(JST).timestamp(),
        'trade_timestamp_jst': datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
        'risk_diff': sl_diff, # USDT換算前の価格差 (ロット計算に使用)
    }
    
    return signal_data


# ====================================================================================
# TRADE EXECUTION & LOGIC
# ====================================================================================

def calculate_trade_size(signal: Dict, account_equity: float, min_amount: float, price_tick_size: float, min_notional_value: float) -> Tuple[float, float, str]:
    """
    リスクベースの動的ポジションサイジングを適用し、取引サイズ (数量) を計算する。
    最小取引単位 (Min Amount) と最小名目価値 (Min Notional Value) のチェックも行う。
    
    引数:
        signal (Dict): 分析シグナルデータ (risk_diff, side, entry_priceを含む)
        account_equity (float): 現在の総資産 (Equity, USDT)
        min_amount (float): 取引所が定める最小取引単位 (数量)
        price_tick_size (float): 価格の最小単位 (丸め処理に使用)
        min_notional_value (float): 取引所が定める最小名目価値 (USDT)
        
    戻り値:
        Tuple[float, float, str]: lot_size (数量), notional_value (名目価値), error_message
    """
    
    risk_diff = signal['risk_diff'] # SLまでの価格差
    entry_price = signal['entry_price']
    
    # 1. 最大リスク額 (USDT) の決定
    # 総資産の MAX_RISK_PER_TRADE_PERCENT 
    max_risk_usdt = account_equity * MAX_RISK_PER_TRADE_PERCENT
    
    if max_risk_usdt < 1.0: # 最低リスク許容額 (例: 1 USDT)
        return 0.0, 0.0, "リスク許容額が低すぎます (Min: 1 USDT)."
        
    # 2. ポジションサイズ (数量) の計算
    # リスク額 = 数量 * 価格差 (リスク) 
    # 数量 (lot_size) = リスク額 / 価格差 (リスク)
    
    # 💡 算出された数量 (単位)
    lot_size_raw = max_risk_usdt / risk_diff
    
    # 3. 数量の丸め処理 (取引所の最小単位に合わせる)
    # CCXTのマーケット情報から取得したロットサイズ (最小数量) の精度に丸める必要がある
    # 例: price_tick_size (0.001) を使用して、lot_size_rawを丸める
    
    # 💡 小数点以下の桁数を計算
    # price_tick_sizeから、数量を丸めるべき桁数を決定。
    # 例: price_tick_size=0.01 の場合、1/0.01=100. 100の対数log10(100)=2 -> 小数点以下2桁
    if price_tick_size > 0:
        precision_digits = int(round(-math.log10(price_tick_size))) 
    else:
        # price_tick_sizeがゼロの場合は、安全のために適当な精度を設定
        precision_digits = 4 
    
    # 丸め処理
    lot_size = round(lot_size_raw, precision_digits)
    
    # 4. 名目価値 (Notional Value, USDT) の計算
    # 名目価値 = 数量 * エントリー価格
    notional_value = lot_size * entry_price
    
    # --- 5. 最小取引単位 (Min Amount) のチェック ---
    if lot_size < min_amount:
        # 💡 Min Amount チェック失敗
        return 0.0, 0.0, f"数量 {lot_size:.4f} が最小取引単位 {min_amount} を下回っています (Min Amount Error)."
        
    # --- 6. 最小名目価値 (Min Notional Value) のチェック (Patch 71 FIX) ---
    # MEXCなどで Code 400 (Min Notional Value Error) が発生するのを防ぐ
    if min_notional_value > 0 and notional_value < min_notional_value:
        # 💡 Min Notional Value チェック失敗
        return 0.0, 0.0, f"名目価値 {format_usdt(notional_value)} USDT が最小名目価値 {format_usdt(min_notional_value)} USDT を下回っています (Min Notional Value Error: Code 400)."

    # 7. 計算されたリスク額 (ロットサイズに基づく実際の額)
    # 実際のロットサイズで計算し直したリスク額をシグナルデータに追加 (通知用)
    actual_risk_usdt = lot_size * risk_diff
    signal['lot_size_units'] = lot_size
    signal['notional_value'] = notional_value
    signal['risk_usdt'] = actual_risk_usdt
    
    return lot_size, notional_value, "ok"

async def execute_trade_logic(signal: Dict, lot_size: float, notional_value: float) -> Dict:
    """
    計算されたロットサイズで成行注文を実行し、結果を返す。
    """
    global EXCHANGE_CLIENT

    symbol = signal['symbol']
    side = signal['side']
    # 'buy' (long) or 'sell' (short)
    order_side = 'buy' if side == 'long' else 'sell' 
    # 'limit', 'market' 
    order_type = 'market' 
    
    # lot_size (数量) を使用
    amount = lot_size 
    
    if TEST_MODE or lot_size <= 0:
        return {
            'status': 'ok',
            'filled_amount': lot_size,
            'filled_usdt': notional_value,
            'entry_price': signal['entry_price'],
            'order_id': f"TEST-{uuid.uuid4()}",
            'error_message': None,
            'is_test': True
        }

    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        return {'status': 'error', 'error_message': 'CCXTクライアントが準備できていません。', 'is_test': False}

    try:
        logging.info(f"🚀 {symbol} - {side.upper()}: 成行注文を試行中... (数量: {amount:.4f}, 名目価値: {format_usdt(notional_value)} USDT)")

        # 💡 成行注文の実行
        order = await EXCHANGE_CLIENT.create_order(
            symbol=symbol,
            type=order_type,
            side=order_side,
            amount=amount,
            params={
                # 先物取引のパラメータ (取引所によって異なる)
            }
        )

        # 注文結果を解析
        filled_amount = order.get('filled', 0.0)
        entry_price = order.get('price', signal['entry_price']) # 約定価格
        
        # 約定額 (filled_usdt) は、取引所APIの応答から取得できない場合、計算値を使用
        filled_usdt_notional = filled_amount * entry_price
        
        if filled_amount > 0 and filled_usdt_notional >= 0.01:
            logging.info(f"✅ {symbol} - {side.upper()}: 注文成功。約定価格 {format_price(entry_price)}, 数量 {filled_amount:.4f}。")
            return {
                'status': 'ok',
                'filled_amount': filled_amount,
                'filled_usdt': filled_usdt_notional,
                'entry_price': entry_price,
                'order_id': order.get('id'),
                'error_message': None,
                'is_test': False
            }
        else:
            # 部分約定または約定失敗
            error_msg = f"注文は受け付けられましたが、約定数量がゼロまたは非常に小さいです (Filled: {filled_amount:.6f} / Expected: {amount:.6f})"
            logging.error(f"❌ {symbol} - {side.upper()} 注文失敗: {error_msg}")
            return {'status': 'error', 'error_message': error_msg, 'is_test': False}


    except ccxt.ExchangeNotAvailable as e:
        error_msg = f"取引所が利用できません: {e}"
        logging.critical(f"❌ {symbol} 注文失敗: {error_msg}")
        return {'status': 'error', 'error_message': error_msg, 'is_test': False}
    except ccxt.DDoSProtection as e:
        error_msg = f"DDoS保護エラー (レート制限): {e}"
        logging.error(f"❌ {symbol} 注文失敗: {error_msg}")
        return {'status': 'error', 'error_message': error_msg, 'is_test': False}
    except ccxt.AuthenticationError as e:
        error_msg = f"認証エラー: {e}"
        logging.critical(f"❌ {symbol} 注文失敗: {error_msg}")
        return {'status': 'error', 'error_message': error_msg, 'is_test': False}
    except ccxt.ExchangeError as e:
        error_str = str(e)
        
        # 💡 特定の取引所エラーコードのチェック
        if '10007' in error_str or 'symbol not supported' in error_str:
            error_msg = "取引所エラー: シンボルがサポートされていません (Code 10007相当)。"
            logging.warning(f"⚠️ {symbol} 注文スキップ: {error_msg}")
        elif '30005' in error_str:
             error_msg = "取引所エラー: 流動性不足 (Code 30005相当)。"
             logging.warning(f"⚠️ {symbol} 注文スキップ: {error_msg}")
        elif '400' in error_str and 'notional' in error_str:
             # Patch 71: calculate_trade_size で Min Notional Value をチェックしているため、
             # ここで捕まることは稀だが、万が一のためにログを強化
             error_msg = "取引所エラー: 最小名目価値エラー (Code 400)。ロットサイズが小さすぎます。"
             logging.error(f"❌ {symbol} 注文失敗: {error_msg}")
        else:
            error_msg = f"その他の取引所エラー: {error_str}"
            logging.error(f"❌ {symbol} 注文失敗: {error_msg}", exc_info=True)
            
        return {'status': 'error', 'error_message': error_msg, 'is_test': False}
    except ccxt.NetworkError as e:
        error_msg = f"ネットワークエラー: {e}"
        logging.error(f"❌ {symbol} 注文失敗: {error_msg}")
        return {'status': 'error', 'error_message': error_msg, 'is_test': False}
    except Exception as e:
        error_msg = f"予期せぬエラー: {e}"
        logging.critical(f"❌ {symbol} 注文失敗 - 予期せぬエラー: {e}", exc_info=True)
        return {'status': 'error', 'error_message': error_msg, 'is_test': False}


async def close_position(position: Dict, exit_type: str, exit_price: float = 0.0) -> Dict:
    """
    ポジションを決済するための成行注文を実行し、結果を返す。
    """
    global EXCHANGE_CLIENT

    symbol = position['symbol']
    side = position['side'] # 'long' or 'short'
    amount = position['contracts'] # 保有している数量
    
    # 決済注文のサイドは、保有ポジションの反対
    # longポジションを決済 -> 'sell'
    # shortポジションを決済 -> 'buy'
    order_side = 'sell' if side == 'long' else 'buy' 
    order_type = 'market' 
    
    if TEST_MODE or amount <= 0:
        pnl_rate = 0.0
        pnl_usdt = 0.0
        if exit_price > 0.0:
            entry_price = position['entry_price']
            filled_usdt = position['filled_usdt']
            
            # PnL計算
            price_diff = (exit_price - entry_price) if side == 'long' else (entry_price - exit_price)
            pnl_usdt = price_diff * amount
            
            # PnL率はレバレッジを考慮しない (filled_usdtベース)
            pnl_rate = pnl_usdt / filled_usdt if filled_usdt > 0 else 0.0
            
        return {
            'status': 'ok',
            'filled_amount': amount,
            'exit_price': exit_price,
            'entry_price': position['entry_price'],
            'pnl_usdt': pnl_usdt,
            'pnl_rate': pnl_rate,
            'exit_type': exit_type,
            'is_test': True
        }

    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        return {'status': 'error', 'error_message': 'CCXTクライアントが準備できていません。', 'exit_type': exit_type}

    try:
        logging.info(f"🔴 {symbol} - {side.upper()} ポジションを {exit_type} で決済 ({amount:.4f} 単位)...")

        # 💡 成行注文の実行 (全量決済)
        order = await EXCHANGE_CLIENT.create_order(
            symbol=symbol,
            type=order_type,
            side=order_side,
            amount=amount, # 全量
            params={
                # 'close_position' のようなパラメータは取引所によって異なる
            }
        )

        # 注文結果を解析
        filled_amount = order.get('filled', 0.0)
        # 決済価格 (API応答から取得。価格がなければ、引数で渡された監視価格を使用)
        exit_price_final = order.get('price', order.get('average', exit_price)) 
        
        entry_price = position['entry_price']
        filled_usdt = position['filled_usdt'] # 注文時の名目価値 (PnL計算に使用)
        
        if filled_amount > 0 and exit_price_final > 0:
            
            # PnLの計算 (価格差 * 数量)
            # long: (決済価格 - エントリー価格) * 数量
            # short: (エントリー価格 - 決済価格) * 数量
            price_diff = (exit_price_final - entry_price) if side == 'long' else (entry_price - exit_price_final)
            pnl_usdt = price_diff * filled_amount
            
            # PnL率 (PnL / 名目価値)
            pnl_rate = pnl_usdt / filled_usdt if filled_usdt > 0 else 0.0
            
            logging.info(
                f"✅ {symbol} 決済成功: 価格 {format_price(exit_price_final)}, PnL {pnl_usdt:+.2f} USDT ({pnl_rate*100:.2f} %)。"
            )
            return {
                'status': 'ok',
                'filled_amount': filled_amount,
                'exit_price': exit_price_final,
                'entry_price': entry_price,
                'pnl_usdt': pnl_usdt,
                'pnl_rate': pnl_rate,
                'exit_type': exit_type
            }
        else:
            error_msg = f"決済注文は受け付けられましたが、約定数量または決済価格が不正です (Filled: {filled_amount:.6f}, Price: {exit_price_final:.2f})"
            logging.error(f"❌ {symbol} 決済失敗: {error_msg}")
            return {'status': 'error', 'error_message': error_msg, 'exit_type': exit_type}

    except ccxt.ExchangeError as e:
        error_msg = f"取引所エラー: {str(e)}"
        logging.error(f"❌ {symbol} 決済失敗: {error_msg}")
        return {'status': 'error', 'error_message': error_msg, 'exit_type': exit_type}
    except ccxt.NetworkError as e:
        error_msg = f"ネットワークエラー: {e}"
        logging.error(f"❌ {symbol} 決済失敗: {error_msg}")
        return {'status': 'error', 'error_message': error_msg, 'exit_type': exit_type}
    except Exception as e:
        error_msg = f"予期せぬエラー: {e}"
        logging.critical(f"❌ {symbol} 決済失敗 - 予期せぬエラー: {e}", exc_info=True)
        return {'status': 'error', 'error_message': error_msg, 'exit_type': exit_type}


# ====================================================================================
# MAIN BOT LOGIC
# ====================================================================================

async def position_monitoring_task():
    """
    ポジションを監視し、SL/TPのトリガーを確認するタスク。
    """
    global EXCHANGE_CLIENT, OPEN_POSITIONS
    
    while True:
        await asyncio.sleep(MONITOR_INTERVAL)
        
        if not OPEN_POSITIONS:
            # 監視するポジションがなければ次のループへ
            continue
            
        if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
            logging.error("❌ ポジション監視失敗: CCXTクライアントが準備できていません。")
            continue

        symbols_to_check = [p['symbol'] for p in OPEN_POSITIONS]
        
        try:
            # 💡 現在の価格をバッチで取得
            tickers = await EXCHANGE_CLIENT.fetch_tickers(symbols_to_check)
        except Exception as e:
            logging.error(f"❌ 価格取得中にエラーが発生しました。次のサイクルで再試行します: {e}")
            continue

        positions_to_close = []
        
        # 💡 ポジションのクローンを作成し、ループ中に変更があっても安全にする
        current_open_positions = OPEN_POSITIONS.copy()
        
        for position in current_open_positions:
            symbol = position['symbol']
            
            ticker = tickers.get(symbol)
            if not ticker or 'last' not in ticker:
                logging.warning(f"⚠️ {symbol} の最新価格情報が取得できませんでした。監視をスキップします。")
                continue
                
            current_price = ticker['last']
            
            sl = position['stop_loss']
            tp = position['take_profit']
            side = position['side']
            
            trigger_type = None
            
            if side == 'long':
                # ロングポジション: 価格がSL以下でSLトリガー、TP以上でTPトリガー
                if current_price <= sl:
                    trigger_type = 'Stop Loss'
                elif current_price >= tp:
                    trigger_type = 'Take Profit'
            
            elif side == 'short':
                # ショートポジション: 価格がSL以上でSLトリガー、TP以下でTPトリガー
                if current_price >= sl:
                    trigger_type = 'Stop Loss'
                elif current_price <= tp:
                    trigger_type = 'Take Profit'
            
            # トリガーされた場合
            if trigger_type:
                positions_to_close.append({
                    'position': position,
                    'exit_type': trigger_type,
                    'exit_price': current_price
                })
        
        # --- 決済の実行 ---
        for item in positions_to_close:
            position = item['position']
            exit_type = item['exit_type']
            exit_price = item['exit_price']
            
            # 決済処理
            close_result = await close_position(position, exit_type, exit_price)
            
            if close_result['status'] == 'ok':
                # ログ記録と通知
                log_signal(position, "ポジション決済", close_result)
                message = format_telegram_message(position, "ポジション決済", get_current_threshold(GLOBAL_MACRO_CONTEXT), trade_result=close_result, exit_type=exit_type)
                await send_telegram_notification(message)
                
                # グローバルなOPEN_POSITIONSリストから削除 (ここでリストを変更する)
                try:
                    OPEN_POSITIONS.remove(position)
                    logging.info(f"✅ {position['symbol']} を管理リストから削除しました。")
                except ValueError:
                    # 既に他の決済処理で削除されている場合がある (稀なケース)
                    logging.warning(f"⚠️ {position['symbol']} は既に管理リストにありませんでした。")
                    
                # WebShareを更新
                webshare_data = {
                    'type': 'exit',
                    'signal_data': position,
                    'trade_result': close_result,
                }
                await send_webshare_update(webshare_data)
                    
            else:
                logging.error(f"❌ {position['symbol']} の {exit_type} 決済に失敗しました: {close_result.get('error_message')}")

async def main_bot_loop():
    """
    ボットのメインロジックを含む非同期ループ。
    """
    global LAST_SUCCESS_TIME, IS_FIRST_MAIN_LOOP_COMPLETED, LAST_ANALYSIS_ONLY_NOTIFICATION_TIME, LAST_WEBSHARE_UPLOAD_TIME, LAST_ANALYSIS_SIGNALS

    # 1. 初期化と市場情報のロード
    is_client_ok = await initialize_exchange_client()
    if not is_client_ok:
        logging.critical("❌ CCXTクライアントの初期化に失敗しました。BOTを終了します。")
        sys.exit(1)
        
    # 2. 初期 FGI の取得
    await calculate_fgi()
    
    # 3. 初期口座ステータスの取得と起動通知
    account_status = await fetch_account_status()
    current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
    startup_message = format_startup_message(account_status, GLOBAL_MACRO_CONTEXT, len(CURRENT_MONITOR_SYMBOLS), current_threshold)
    await send_telegram_notification(startup_message)

    # 4. メインループ
    while True:
        try:
            # --- 4-1. FGIと市場環境の更新 (1時間ごと) ---
            if time.time() - LAST_ANALYSIS_ONLY_NOTIFICATION_TIME >= ANALYSIS_ONLY_INTERVAL:
                await calculate_fgi()
                await get_top_volume_symbols()
                
                # 資産状況も更新
                account_status = await fetch_account_status()
                current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
                
                # 分析専用通知 (銘柄分析は後で実行されるため、ここでは省略)
                LAST_ANALYSIS_ONLY_NOTIFICATION_TIME = time.time()

            # --- 4-2. 銘柄分析の実行 ---
            logging.info(f"\n--- 📈 メイン分析ループ開始 (時刻: {datetime.now(JST).strftime('%H:%M:%S')}) ---")
            
            current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
            
            # 監視対象銘柄のリスト
            symbols_to_analyze = CURRENT_MONITOR_SYMBOLS
            
            # 全ての銘柄と時間枠で分析を実行
            analysis_tasks = []
            for symbol in symbols_to_analyze:
                for tf in TARGET_TIMEFRAMES:
                    # 💡 タスクを作成し、並行して実行
                    analysis_tasks.append(
                        analyze_symbol_timeframe(symbol, tf, current_threshold)
                    )
                    
            # 並行実行し、結果を待つ
            # 実行時間が長くなる可能性を考慮し、timeoutは設定しない
            results = await asyncio.gather(*analysis_tasks)
            
            # 結果をフィルタリングし、有効なシグナルのみを抽出
            valid_signals = [res for res in results if res is not None]
            
            # スコア順にソート (降順)
            valid_signals.sort(key=lambda x: x['score'], reverse=True)
            LAST_ANALYSIS_SIGNALS = valid_signals # WebAPI用にグローバル更新

            # --- 4-3. 取引実行 ---
            
            # 過去12時間以内に取引を行った銘柄を除外し、上位シグナルをフィルタリング
            tradable_signals = []
            for signal in valid_signals:
                symbol = signal['symbol']
                # クールダウン期間のチェック
                if time.time() - LAST_SIGNAL_TIME.get(symbol, 0) < TRADE_SIGNAL_COOLDOWN:
                    continue
                # 既にポジションを持っている銘柄はスキップ
                if symbol in [p['symbol'] for p in OPEN_POSITIONS]:
                    continue
                
                tradable_signals.append(signal)

            
            # 上位1銘柄のみ取引
            if tradable_signals:
                best_signal = tradable_signals[0]
                
                # 💡 取引ロットの計算に必要な情報を取得
                symbol = best_signal['symbol']
                market_info = EXCHANGE_CLIENT.markets.get(symbol)
                
                # market_infoのチェック
                if not market_info:
                    logging.error(f"❌ 取引失敗: {symbol} の市場情報が見つかりません。")
                    
                else:
                    # Min Amount (最小数量)
                    min_amount = market_info.get('limits', {}).get('amount', {}).get('min', 0.0) 
                    # Price Tick Size (価格精度)
                    price_tick_size = market_info.get('precision', {}).get('price', 0.0)
                    # Min Notional Value (最小名目価値) - Patch 71対応
                    min_notional_value = market_info.get('limits', {}).get('cost', {}).get('min', 0.0) 
                    
                    if min_amount is None:
                        # Min AmountがAPI応答になければ、安全のため0.001などを使用
                        min_amount = 0.001 
                    if price_tick_size is None or price_tick_size == 0.0:
                        # Price Tick SizeがAPI応答になければ、安全のため0.01などを使用
                        price_tick_size = 0.01

                    # 💡 ロットサイズと名目価値の計算
                    lot_size, notional_value, size_error = calculate_trade_size(
                        best_signal, 
                        ACCOUNT_EQUITY_USDT, 
                        min_amount,
                        price_tick_size,
                        min_notional_value # Patch 71
                    )
                    
                    if size_error == "ok":
                        # 💡 取引実行
                        trade_result = await execute_trade_logic(best_signal, lot_size, notional_value)
                        
                        # 結果をシグナルに統合し、ログと通知
                        best_signal['trade_result'] = trade_result
                        
                        log_signal(best_signal, "取引シグナル", trade_result)
                        
                        message = format_telegram_message(best_signal, "取引シグナル", current_threshold, trade_result)
                        await send_telegram_notification(message)
                        
                        # 取引が成功した場合、ポジションリストに追加
                        if trade_result['status'] == 'ok' and not trade_result['is_test']:
                            # ポジションを追跡するための情報を保存
                            OPEN_POSITIONS.append({
                                'symbol': symbol,
                                'side': best_signal['side'],
                                'entry_price': trade_result['entry_price'],
                                'contracts': trade_result['filled_amount'],
                                'filled_usdt': trade_result['filled_usdt'],
                                'stop_loss': best_signal['stop_loss'],
                                'take_profit': best_signal['take_profit'],
                                'liquidation_price': best_signal['liquidation_price'],
                            })
                            logging.info(f"✅ {symbol} ポジションを管理リストに追加しました。")
                        
                        # 取引が実行された場合、クールダウンを更新
                        LAST_SIGNAL_TIME[symbol] = time.time()
                        
                        # WebShareを更新
                        webshare_data = {
                            'type': 'trade',
                            'signal_data': best_signal,
                            'trade_result': trade_result,
                        }
                        await send_webshare_update(webshare_data)


                    else:
                        # ロットサイズ計算エラー (Min Amount / Min Notional Value Error: Code 400 相当)
                        logging.warning(f"⚠️ {symbol} - {best_signal['side'].upper()}: ロットサイズ計算エラーのため取引をスキップ: {size_error}")
                        # ログと通知はスキップ (ノイズが大きくなるため)
            
            
            # --- 4-4. WebShareデータのアップロード (1時間ごと) ---
            if time.time() - LAST_WEBSHARE_UPLOAD_TIME >= WEBSHARE_UPLOAD_INTERVAL:
                # ログファイルの内容をロードし、WebShareに送信する処理をここに実装
                # 例: 最後の100シグナルと現在のポジションを送信
                webshare_status_data = {
                    'timestamp_jst': datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
                    'account_equity_usdt': ACCOUNT_EQUITY_USDT,
                    'open_positions_count': len(OPEN_POSITIONS),
                    'open_positions': OPEN_POSITIONS,
                    'last_signals': LAST_ANALYSIS_SIGNALS[:10], # トップ10シグナル
                    'macro_context': GLOBAL_MACRO_CONTEXT,
                }
                await send_webshare_update({'type': 'status', 'data': webshare_status_data})
                LAST_WEBSHARE_UPLOAD_TIME = time.time()
                
            
            # --- 4-5. ループの終了処理 ---
            LAST_SUCCESS_TIME = time.time()
            IS_FIRST_MAIN_LOOP_COMPLETED = True
            logging.info(f"--- 🏁 メイン分析ループ終了。次の実行まで {LOOP_INTERVAL} 秒待機。 ---")

        except Exception as e:
            logging.critical(f"❌ メインループで致命的なエラーが発生しました: {e}", exc_info=True)
            # クライアントが切断された可能性があるため、再初期化を試みる
            await asyncio.sleep(60) # 60秒待機
            await initialize_exchange_client()

        # ループ間隔を待機
        await asyncio.sleep(LOOP_INTERVAL)


async def analyze_symbol_timeframe(symbol: str, timeframe: str, current_threshold: float) -> Optional[Dict]:
    """特定の銘柄と時間枠で分析を実行するヘルパー関数。"""
    
    # 1. OHLCVデータを取得
    df = await get_historical_ohlcv(symbol, timeframe, REQUIRED_OHLCV_LIMITS[timeframe])
    if df is None:
        return None

    # 2. 最新価格を取得 (最後の終値)
    current_price = df['close'].iloc[-1]
    
    # 3. テクニカル指標を計算
    try:
        df = calculate_technical_indicators(df)
    except ValueError as e:
        logging.warning(f"⚠️ {symbol} - {timeframe}: テクニカル指標計算エラー - データが不足しています。スキップします。({e})")
        return None
    except Exception as e:
        logging.error(f"❌ {symbol} - {timeframe}: 予期せぬテクニカル指標計算エラー: {e}")
        return None
        
    # 4. シグナル分析
    signal = analyze_signal(symbol, timeframe, df, current_price, GLOBAL_MACRO_CONTEXT)
    
    if signal and signal['score'] >= current_threshold:
        logging.info(f"🎯 シグナル検出: {symbol} ({timeframe}) - {signal['side'].upper()} Score: {signal['score']*100:.2f}")
        return signal
        
    return None

# ====================================================================================
# FASTAPI ENDPOINTS & STARTUP
# ====================================================================================

@app.get("/", summary="BOTの基本ステータスを取得")
async def get_status():
    """BOTの基本動作状態、最終成功時刻、現在のマクロコンテキストを返す。"""
    
    # 現在の残高を取得
    account_status = await fetch_account_status()
    
    # 現在の取引閾値を取得
    current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
    
    return {
        "status": "Running" if IS_CLIENT_READY else "Error/Initializing",
        "version": "v20.0.25",
        "exchange": CCXT_CLIENT_NAME.upper(),
        "leverage": LEVERAGE,
        "test_mode": TEST_MODE,
        "last_success_time_jst": datetime.fromtimestamp(LAST_SUCCESS_TIME, JST).strftime("%Y-%m-%d %H:%M:%S") if LAST_SUCCESS_TIME > 0 else "N/A",
        "account_equity_usdt": format_usdt(ACCOUNT_EQUITY_USDT),
        "monitoring_symbols_count": len(CURRENT_MONITOR_SYMBOLS),
        "trade_threshold_score": f"{current_threshold*100:.2f} / 100",
        "macro_context": GLOBAL_MACRO_CONTEXT,
    }

@app.get("/signals", summary="最新の分析シグナルリストを取得 (トップ5件)")
async def get_latest_signals():
    """最新の分析で検出されたシグナルを返す (変更なし)"""
    return {
        "timestamp_jst": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
        "total_signals": len(LAST_ANALYSIS_SIGNALS),
        "top_signals": [{
            "symbol": s['symbol'],
            "timeframe": s['timeframe'],
            "score": s['score'],
            "side": s['side'],
            "rr_ratio": s['rr_ratio'],
        } for s in LAST_ANALYSIS_SIGNALS[:5]]
    }

@app.get("/positions", summary="現在BOTが管理しているオープンポジションを取得")
async def get_open_positions():
    """現在BOTが管理しているオープンポジションのリストを返す (変更なし)"""
    return {
        "timestamp_jst": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
        "positions": [{
            "symbol": p['symbol'],
            "side": p['side'],
            "entry_price": format_price(p['entry_price']),
            "contracts": f"{p['contracts']:.4f}",
            "notional_value_usdt": format_usdt(p['filled_usdt']),
            "stop_loss": format_price(p['stop_loss']),
            "take_profit": format_price(p['take_profit']),
        } for p in OPEN_POSITIONS]
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
    
    # 💡 BOTタスクがメインスレッドをブロックしないように、
    # asyncio.run で非同期実行の環境を構築し、サーバーをサービスする
    
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info")
    server = uvicorn.Server(config)
    
    # 💡 既にバックグラウンドでタスクを開始するロジックを app.on_event("startup") に定義したため、
    # ここではサーバーを起動するだけで良い
    try:
        asyncio.run(server.serve())
    except KeyboardInterrupt:
        logging.info("👋 BOT APIサーバーをシャットダウンします。")
    except Exception as e:
        logging.critical(f"❌ サーバー起動中に予期せぬエラーが発生しました: {e}", exc_info=True)
        sys.exit(1)
