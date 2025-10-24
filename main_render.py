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
            raise ValueError("FGI APIから不正なデータ構造を受信しました。")
            
        fgi_entry = data['data'][0]
        
        if 'value' not in fgi_entry or 'value_classification' not in fgi_entry:
            raise ValueError("FGIデータエントリに'value'または'value_classification'が見つかりません。")

        # 恐怖・貪欲の生の値を取得
        raw_fgi_value_str = fgi_entry['value']
        raw_fgi_classification = fgi_entry['value_classification']
        
        try:
            raw_fgi_value = float(raw_fgi_value_str)
        except ValueError:
             raise ValueError(f"FGI値 '{raw_fgi_value_str}' が数値に変換できません。")
             
        # FGI値をプロキシスコア (0.00〜0.05) に変換する
        # FGIは0(Extreme Fear)〜100(Extreme Greed)の範囲
        # - 0-10: 最小 (-0.05)
        # - 50: 0.00 (中立)
        # - 90-100: 最大 (+0.05)
        
        # 50を基準 (0点) に、±50の範囲で正規化 (-1.0〜1.0)
        normalized_fgi = (raw_fgi_value - 50.0) / 50.0 
        
        # プロキシスコア (-0.05〜0.05) に変換
        fgi_proxy_score = normalized_fgi * FGI_PROXY_BONUS_MAX
        
        # GLOBAL_MACRO_CONTEXTを更新
        GLOBAL_MACRO_CONTEXT['fgi_proxy'] = fgi_proxy_score
        GLOBAL_MACRO_CONTEXT['fgi_raw_value'] = f"Value: {raw_fgi_value:.0f} / {raw_fgi_classification}"
        
        logging.info(f"✅ FGI取得成功: Raw={raw_fgi_value:.0f} ({raw_fgi_classification}), Proxy={fgi_proxy_score*100:.2f}%")
        
    except (requests.exceptions.RequestException, ValueError, KeyError) as e:
        logging.error(f"❌ FGI取得失敗 (エラー): {e}")
        # 失敗した場合、FGIの影響を中立 (0.0) にリセット
        GLOBAL_MACRO_CONTEXT['fgi_proxy'] = 0.0
        GLOBAL_MACRO_CONTEXT['fgi_raw_value'] = f"取得失敗: {type(e).__name__}"
        await send_telegram_notification(f"🚨 <b>FGI APIエラー</b>\n恐怖・貪欲指数 (FGI) の取得に失敗しました: <code>{type(e).__name__}: {e}</code>")

# --- 修正終了: calculate_fgi関数の追加 (または既存の関数の置き換え) ---


async def fetch_and_prepare_ohlcv(
    symbol: str, 
    timeframe: str, 
    since_timestamp: Optional[int] = None,
    limit: int = 1000
) -> Tuple[Optional[pd.DataFrame], Optional[float]]:
    """
    指定されたシンボルとタイムフレームのOHLCVデータを取得し、DataFrameとして整形する。
    
    Args:
        symbol: 取引シンボル (e.g., 'BTC/USDT')。
        timeframe: タイムフレーム (e.g., '1h', '4h')。
        since_timestamp: 取得開始のUNIXタイムスタンプ (ミリ秒)。Noneの場合は最新から。
        limit: 取得するローソク足の最大数。

    Returns:
        (OHLCV DataFrame, 最新の終値) のタプル。失敗した場合は (None, None)。
    """
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.error("❌ OHLCV取得失敗: CCXTクライアントが準備できていません。")
        return None, None

    try:
        # ccxt.fetch_ohlcv はミリ秒のタイムスタンプを要求
        ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(
            symbol, 
            timeframe, 
            since=since_timestamp, 
            limit=limit
        )
        
        if not ohlcv or len(ohlcv) < 100: # 安定性のために最低100本を要求
            logging.warning(f"⚠️ {symbol} - {timeframe} のデータが不足しています (取得数: {len(ohlcv)})。")
            return None, None

        # DataFrameに変換
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['datetime'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True).dt.tz_convert(JST)
        df = df.set_index('datetime')
        
        # 💡 【Patch 71 FIX: NaN/NoneTypeエラー修正】データ分析の安定性のためにNaN行を除外
        df = df.dropna()
        
        latest_close = df['close'].iloc[-1] if not df.empty else None

        return df, latest_close

    except ccxt.NetworkError as e:
        logging.error(f"❌ OHLCV取得失敗 - ネットワークエラー ({symbol} - {timeframe}): {e}")
        return None, None
    except ccxt.ExchangeError as e:
        error_str = str(e)
        if '10007' in error_str or 'symbol not support api' in error_str:
            logging.warning(f"⚠️ OHLCV取得スキップ - サポート外のシンボル ({symbol} - {timeframe}): {e}")
            # シンボルがサポートされていない場合は致命的なエラーではない
            return None, None
        elif '30005' in error_str:
            logging.warning(f"⚠️ OHLCV取得スキップ - 流動性不足 ({symbol} - {timeframe}): {e}")
            return None, None
        
        logging.error(f"❌ OHLCV取得失敗 - CCXTエラー ({symbol} - {timeframe}): {e}")
        return None, None
    except Exception as e:
        logging.critical(f"❌ OHLCV取得失敗 - 予期せぬエラー ({symbol} - {timeframe}): {e}", exc_info=True)
        return None, None

async def get_markets_and_update_symbols() -> List[str]:
    """
    取引所からアクティブな先物市場を取得し、出来高TOP銘柄で監視リストを更新する。
    """
    global EXCHANGE_CLIENT, CURRENT_MONITOR_SYMBOLS
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.error("❌ マーケット情報更新失敗: CCXTクライアントが準備できていません。デフォルトシンボルを使用します。")
        return CURRENT_MONITOR_SYMBOLS
        
    if SKIP_MARKET_UPDATE:
        logging.warning("⚠️ SKIP_MARKET_UPDATEが有効です。監視シンボルの更新をスキップし、デフォルトリストを使用します。")
        return CURRENT_MONITOR_SYMBOLS.copy()

    try:
        # fetch_markets は既に initialize_exchange_client でロードされているはずですが、
        # ここでは更新とフィルタリングを行います。
        await EXCHANGE_CLIENT.load_markets(reload=True)
        
        # 出来高の多いアクティブなUSDT建て先物市場をフィルタリング
        active_future_markets = {
            s: m for s, m in EXCHANGE_CLIENT.markets.items() 
            if m.get('active') 
            and m.get('quote') == 'USDT' 
            and m.get('type') in ['future', 'swap']
            and m.get('info', {}).get('state', 'NORMAL') == 'NORMAL' # MEXCなどのアクティブチェック
        }
        
        if not active_future_markets:
            logging.warning("⚠️ アクティブなUSDT建て先物市場が見つかりませんでした。デフォルトシンボルを使用します。")
            return CURRENT_MONITOR_SYMBOLS.copy()
            
        logging.info(f"✅ アクティブなUSDT建て先物市場を {len(active_future_markets)} 件検出しました。")
        
        # 出来高情報を取得 (MEXCではfetchTickersが過去24時間の出来高を含む)
        tickers = await EXCHANGE_CLIENT.fetch_tickers(list(active_future_markets.keys()))
        
        # 24時間出来高 (quote volume) でソート
        # 'quoteVolume' は USDT 建ての出来高
        sorted_tickers = sorted(
            [
                (s, t) for s, t in tickers.items() 
                if t and t.get('quoteVolume') is not None
            ],
            key=lambda x: x[1]['quoteVolume'], 
            reverse=True
        )
        
        # トップN銘柄を選出
        top_symbols = [symbol for symbol, _ in sorted_tickers[:TOP_SYMBOL_LIMIT]]
        
        # デフォルトリストと統合し、重複を排除し、最大数に制限
        combined_symbols = list(set(DEFAULT_SYMBOLS + top_symbols))
        final_monitor_symbols = combined_symbols[:TOP_SYMBOL_LIMIT]
        
        # グローバル変数を更新
        CURRENT_MONITOR_SYMBOLS = final_monitor_symbols
        logging.info(f"✅ 監視シンボルリストを更新しました。合計 {len(final_monitor_symbols)} 銘柄 ({TOP_SYMBOL_LIMIT}件に制限)。")
        
        # リストの先頭をログ出力
        logging.info(f"  - Top 5 Symbols: {final_monitor_symbols[:5]}")

        return final_monitor_symbols

    except ccxt.ExchangeNotAvailable as e:
        logging.error(f"❌ マーケット情報更新失敗 - 取引所接続エラー: {e}")
    except ccxt.NetworkError as e:
        logging.error(f"❌ マーケット情報更新失敗 - ネットワークエラー: {e}")
    except Exception as e:
        logging.critical(f"❌ マーケット情報更新失敗 - 予期せぬエラー: {e}", exc_info=True)
        
    # エラー時は既存またはデフォルトのリストを返す
    return CURRENT_MONITOR_SYMBOLS.copy()


# ====================================================================================
# TECHNICAL ANALYSIS & SCORING LOGIC
# ====================================================================================

def calculate_volatility_score(df: pd.DataFrame, current_price: float) -> Tuple[float, Optional[float], Optional[float]]:
    """
    ボラティリティ指標 (ATR, ボリンジャーバンド幅) に基づいてリスクとペナルティを計算する。
    """
    if len(df) < ATR_LENGTH:
        return 0.0, None, None # データ不足
        
    # 1. ATR (Average True Range) を計算
    atr_series = ta.atr(df['high'], df['low'], df['close'], length=ATR_LENGTH)
    current_atr = atr_series.iloc[-1] if not atr_series.empty else None
    
    if current_atr is None or current_atr <= 0:
        return 0.0, None, None
        
    # 2. ボリンジャーバンド (BBAND) を計算
    # デフォルトの期間 (length=20), 標準偏差 (std=2.0)
    bbands = ta.bbands(df['close'], length=20, std=2.0)
    
    # BBAND幅の計算: (UBAND - LBAND) / Close
    bb_width = bbands[f'BBL_20_2.0'].iloc[-1] - bbands[f'BBU_20_2.0'].iloc[-1]
    bb_percent_width = abs(bb_width) / current_price if current_price else 0.0
    
    # 3. ボラティリティ過熱ペナルティの計算
    volatility_penalty = 0.0
    if bb_percent_width > VOLATILITY_BB_PENALTY_THRESHOLD: # 例: 1%を超えるBB幅
        # 幅が広いほどペナルティを増加させる (最大ペナルティは -0.10)
        penalty_factor = min(1.0, (bb_percent_width - VOLATILITY_BB_PENALTY_THRESHOLD) / (VOLATILITY_BB_PENALTY_THRESHOLD * 2))
        volatility_penalty = -0.10 * penalty_factor
        
    return volatility_penalty, current_atr, bb_percent_width

def calculate_risk_reward(
    current_price: float, 
    side: str, 
    atr_value: float
) -> Tuple[float, float, float]:
    """
    ATRに基づいた動的なSL/TPを計算し、リスクリワード比率 (R:R) を返す。
    SLはATRの2.0倍、TPはSLの1.5倍に設定。
    """
    
    if atr_value is None or atr_value <= 0:
        # ATR計算失敗時はデフォルトの小さいリスク幅を使用
        default_risk_percent = MIN_RISK_PERCENT 
        if side.lower() == 'long':
            stop_loss = current_price * (1 - default_risk_percent)
            risk_width_price = current_price - stop_loss
            take_profit = current_price * (1 + default_risk_percent * 1.5)
            reward_width_price = take_profit - current_price
        else: # short
            stop_loss = current_price * (1 + default_risk_percent)
            risk_width_price = stop_loss - current_price
            take_profit = current_price * (1 - default_risk_percent * 1.5)
            reward_width_price = current_price - take_profit

    else:
        # SLをATRの ATR_MULTIPLIER_SL 倍に設定
        sl_distance = atr_value * ATR_MULTIPLIER_SL
        
        # 最小リスク幅の強制 (例: 0.8%)
        min_risk_distance = current_price * MIN_RISK_PERCENT
        sl_distance = max(sl_distance, min_risk_distance)

        # TPをSLの1.5倍の距離に設定
        tp_distance = sl_distance * 1.5 
        
        if side.lower() == 'long':
            stop_loss = current_price - sl_distance
            take_profit = current_price + tp_distance
            risk_width_price = sl_distance
            reward_width_price = tp_distance
        else: # short
            stop_loss = current_price + sl_distance
            take_profit = current_price - tp_distance
            risk_width_price = sl_distance
            reward_width_price = tp_distance
            
    # リスクリワード比率 (R:R) の計算
    # Risk: Reward = 1 : (Reward_Width / Risk_Width)
    if risk_width_price <= 0:
        rr_ratio = 1.0 # ゼロ除算回避
    else:
        rr_ratio = reward_width_price / risk_width_price
        
    return stop_loss, take_profit, rr_ratio


def analyze_technical_indicators(
    df_m: Dict[str, pd.DataFrame], 
    current_prices: Dict[str, float],
    symbol: str, 
    side: str,
    macro_context: Dict
) -> Optional[Dict]:
    """
    マルチタイムフレームのOHLCVデータとテクニカル指標に基づいてスコアリングを行う。
    
    Args:
        df_m: タイムフレームごとのOHLCV DataFrameの辞書。
        current_prices: タイムフレームごとの最新終値の辞書。
        symbol: 現在分析中のシンボル。
        side: 信号の方向 ('long' または 'short')。
        macro_context: グローバルマクロコンテキスト。
        
    Returns:
        シグナル情報 (Dict) または None。
    """
    
    # 💡 分析対象は 15分足 をベースとする (最も動的な信号を発するため)
    BASE_TIMEFRAME = '15m'
    
    # 必須のデータが存在するかチェック
    if BASE_TIMEFRAME not in df_m or df_m[BASE_TIMEFRAME] is None:
        logging.warning(f"⚠️ {symbol} - {BASE_TIMEFRAME} のデータが利用できません。分析をスキップします。")
        return None
        
    df = df_m[BASE_TIMEFRAME]
    if len(df) < LONG_TERM_SMA_LENGTH:
        logging.warning(f"⚠️ {symbol} - {BASE_TIMEFRAME} のデータが不足しています (SMA {LONG_TERM_SMA_LENGTH}期間未満)。分析をスキップします。")
        return None
        
    current_price = current_prices.get(BASE_TIMEFRAME, df['close'].iloc[-1])
    
    # ===============================================================
    # 1. 共通指標の計算
    # ===============================================================
    
    # Long-term SMA (200期間)
    df['SMA_Long'] = ta.sma(df['close'], length=LONG_TERM_SMA_LENGTH)
    
    # RSI (14期間)
    df['RSI'] = ta.rsi(df['close'], length=14)
    
    # MACD (12, 26, 9)
    macd_output = ta.macd(df['close'], fast=12, slow=26, signal=9)
    df['MACD'] = macd_output[f'MACD_12_26_9']
    df['MACDh'] = macd_output[f'MACDh_12_26_9'] # ヒストグラム
    df['MACDs'] = macd_output[f'MACDs_12_26_9'] # シグナルライン
    
    # OBV (On-Balance Volume)
    df['OBV'] = ta.obv(df['close'], df['volume'])
    
    # ATR & Volatility (14期間)
    volatility_penalty, current_atr, bb_percent_width = calculate_volatility_score(df, current_price)
    
    if current_atr is None:
        logging.warning(f"⚠️ {symbol} - {BASE_TIMEFRAME} のATR計算に失敗しました。分析をスキップします。")
        return None
        
    # ===============================================================
    # 2. スコアリングロジックの適用 (Base Score: 0.40)
    # ===============================================================
    
    score = BASE_SCORE
    tech_data: Dict[str, Any] = {
        'base_score': BASE_SCORE,
        'long_term_reversal_penalty_value': 0.0,
        'structural_pivot_bonus': 0.0,
        'macd_penalty_value': 0.0,
        'liquidity_bonus_value': 0.0,
        'sentiment_fgi_proxy_bonus': 0.0,
        'forex_bonus': 0.0,
        'obv_momentum_bonus_value': 0.0,
        'volatility_penalty_value': volatility_penalty,
    }

    
    # --- A. 長期トレンドの確認 (SMA 200) ---
    # 順張り方向で有利な場合に加点し、逆張りの場合にペナルティを適用するロジック
    
    # 最新のローソク足の終値と長期SMAを比較
    latest_close = df['close'].iloc[-1]
    latest_sma_long = df['SMA_Long'].iloc[-1]
    
    if latest_sma_long is not None:
        if side == 'long':
            # Long: 価格が長期SMAの上にある場合に加点 (逆行ペナルティを回避)
            if latest_close > latest_sma_long:
                score += LONG_TERM_REVERSAL_PENALTY 
            else:
                # 逆行の場合、ペナルティ
                score -= LONG_TERM_REVERSAL_PENALTY 
                tech_data['long_term_reversal_penalty_value'] = LONG_TERM_REVERSAL_PENALTY
        else: # side == 'short'
            # Short: 価格が長期SMAの下にある場合に加点 (逆行ペナルティを回避)
            if latest_close < latest_sma_long:
                score += LONG_TERM_REVERSAL_PENALTY 
            else:
                # 逆行の場合、ペナルティ
                score -= LONG_TERM_REVERSAL_PENALTY 
                tech_data['long_term_reversal_penalty_value'] = LONG_TERM_REVERSAL_PENALTY
        
        # ちなみに、このロジックでは Long Term SMAの方向性 (傾き) は考慮していません。
    
    # --- B. 価格構造/ピボットの支持 (Pivot Point) ---
    # 直近のローソク足のロウソク足パターンを確認し、サイド方向に有利な構造があればボーナス
    # ここでは、簡略化のため、Long Term SMAに近いか、直近の高値/安値を更新しているか、などの簡易チェックを行う。
    
    # 過去3期間のClose
    closes = df['close'].iloc[-4:].tolist()
    is_favorable_pivot = False
    
    if len(closes) == 4:
        if side == 'long':
            # ロングの場合: 直前の安値からの反転、または直前の抵抗をブレイク
            # 例: 3本前の安値 > 2本前の安値 < 1本前の安値 (ダブルボトム的な構造)
            lows = df['low'].iloc[-4:].tolist()
            if lows[1] < lows[2] and lows[2] < lows[3]:
                is_favorable_pivot = True
            elif latest_close > max(closes[:-1]): # 抵抗をブレイク
                 is_favorable_pivot = True
            
        else: # side == 'short'
            # ショートの場合: 直前の高値からの反転、または直前の支持をブレイク
            # 例: 3本前の高値 < 2本前の高値 > 1本前の高値 (ダブルトップ的な構造)
            highs = df['high'].iloc[-4:].tolist()
            if highs[1] > highs[2] and highs[2] > highs[3]:
                is_favorable_pivot = True
            elif latest_close < min(closes[:-1]): # 支持をブレイク
                 is_favorable_pivot = True
                 
    if is_favorable_pivot:
        score += STRUCTURAL_PIVOT_BONUS
        tech_data['structural_pivot_bonus'] = STRUCTURAL_PIVOT_BONUS


    # --- C. モメンタムの確認 (MACD クロス) ---
    # MACDがシグナルラインをサイド方向と逆行してクロスした場合にペナルティ
    
    latest_macd = df['MACD'].iloc[-1]
    latest_macds = df['MACDs'].iloc[-1]
    
    # MACDラインがシグナルラインより下にある (デッドクロス) = ロングに不利
    is_dead_cross = latest_macd < latest_macds 
    
    if side == 'long' and is_dead_cross:
        score -= MACD_CROSS_PENALTY
        tech_data['macd_penalty_value'] = MACD_CROSS_PENALTY
        
    elif side == 'short' and not is_dead_cross: # ゴールデンクロス = ショートに不利
        score -= MACD_CROSS_PENALTY
        tech_data['macd_penalty_value'] = MACD_CROSS_PENALTY
        
    
    # --- D. 出来高の確証 (OBV) ---
    # OBVが最新のローソク足でサイド方向に動いている場合にボーナス
    latest_obv = df['OBV'].iloc[-1]
    prev_obv = df['OBV'].iloc[-2]
    
    if side == 'long' and latest_obv > prev_obv:
        score += OBV_MOMENTUM_BONUS
        tech_data['obv_momentum_bonus_value'] = OBV_MOMENTUM_BONUS
    elif side == 'short' and latest_obv < prev_obv:
        score += OBV_MOMENTUM_BONUS
        tech_data['obv_momentum_bonus_value'] = OBV_MOMENTUM_BONUS
        
        
    # --- E. Volatility Penalty (ボラティリティ過熱ペナルティ) ---
    # calculate_volatility_score で計算済み
    score += volatility_penalty
    
    
    # --- F. Liquidity Bonus (流動性ボーナス) ---
    # 出来高 (Quote Volume) に基づいてボーナスを付与 (市場監視リスト更新時に使用した出来高を使用)
    # ここでは、簡易的に出来高を評価する。
    latest_volume = df['volume'].iloc[-1]
    
    # 過去30期間の平均出来高
    avg_volume = df['volume'].iloc[-30:].mean()
    
    liquidity_bonus = 0.0
    if latest_volume > avg_volume * 1.5: # 平均の1.5倍以上の出来高
        liquidity_bonus = LIQUIDITY_BONUS_MAX * 0.5
    elif latest_volume > avg_volume * 2.0: # 平均の2倍以上の出来高
        liquidity_bonus = LIQUIDITY_BONUS_MAX
        
    score += liquidity_bonus
    tech_data['liquidity_bonus_value'] = liquidity_bonus
    
    
    # --- G. FGI/Macro Bonus (マクロ影響) ---
    fgi_proxy = macro_context.get('fgi_proxy', 0.0)
    
    fgi_bonus_applied = 0.0
    # Longの場合: FGIがプラス (Greed) 方向でボーナス、マイナス (Fear) 方向でペナルティ
    if side == 'long':
        fgi_bonus_applied = fgi_proxy # Max +0.05 / Min -0.05
    # Shortの場合: FGIがマイナス (Fear) 方向でボーナス、プラス (Greed) 方向でペナルティ
    elif side == 'short':
        fgi_bonus_applied = -fgi_proxy # Max +0.05 / Min -0.05 (Fearでプラスになる)
        
    score += fgi_bonus_applied
    tech_data['sentiment_fgi_proxy_bonus'] = fgi_bonus_applied
    
    
    # --- H. Forex Bonus (為替マクロ影響) ---
    # 機能削除済みのボーナスをゼロとして維持 (互換性のため)
    forex_bonus = macro_context.get('forex_bonus', 0.0)
    score += forex_bonus
    tech_data['forex_bonus'] = forex_bonus
    
    # ===============================================================
    # 3. リスク・リワードと最終調整
    # ===============================================================

    # SL/TPとRRRの計算
    stop_loss, take_profit, rr_ratio = calculate_risk_reward(current_price, side, current_atr)

    # RRRが極端に低い場合 (例: 1.0未満) は、スコアを大きく減点 (最大-0.10)
    rr_penalty = 0.0
    if rr_ratio < 1.0:
        rr_penalty = -0.10 * (1.0 - rr_ratio) # RRR 0.5 の場合、-0.05
        score += rr_penalty

    # 最終スコアを 0.0〜1.0 の範囲にクリップ
    final_score = max(0.0, min(1.0, score))

    # シグナル情報として返す
    signal_data = {
        'symbol': symbol,
        'timeframe': BASE_TIMEFRAME,
        'side': side,
        'score': final_score,
        'rr_ratio': rr_ratio,
        'current_price': current_price,
        'entry_price': current_price, # 成行注文を想定
        'stop_loss': stop_loss,
        'take_profit': take_profit,
        'liquidation_price': calculate_liquidation_price(current_price, LEVERAGE, side, MIN_MAINTENANCE_MARGIN_RATE),
        'tech_data': tech_data,
        'atr_value': current_atr
    }
    
    return signal_data


def find_top_signals(
    monitoring_symbols: List[str], 
    target_timeframes: List[str],
    macro_context: Dict
) -> List[Dict]:
    """
    全ての銘柄・タイムフレームで分析を実行し、基準以上のスコアを持つトップシグナルを返す。
    """
    
    all_signals: List[Dict] = []
    
    # 動的な取引閾値を決定
    current_threshold = get_current_threshold(macro_context)

    logging.info(f"🔍 分析を開始します。取引閾値 (動的): {current_threshold*100:.2f} %")
    
    # 1. すべてのOHLCVデータを取得
    # マルチタイムフレームのデータを非同期で取得するため、タスクリストを作成
    ohlcv_tasks = []
    ohlcv_data_cache: Dict[str, Dict[str, pd.DataFrame]] = {} # symbol -> timeframe -> df
    price_cache: Dict[str, Dict[str, float]] = {} # symbol -> timeframe -> price
    
    # すべてのシンボル/タイムフレームのOHLCV取得タスクを作成
    for symbol in monitoring_symbols:
        ohlcv_data_cache[symbol] = {}
        price_cache[symbol] = {}
        
        for tf in target_timeframes:
            limit = REQUIRED_OHLCV_LIMITS.get(tf, 1000)
            ohlcv_tasks.append(
                fetch_and_prepare_ohlcv(symbol, tf, limit=limit)
            )
            
    # すべてのOHLCV取得タスクを並行実行
    results = asyncio.gather(*ohlcv_tasks) # タスクを実行待ち
    
    # 結果のパース
    # result_index = 0
    # for symbol in monitoring_symbols:
    #     for tf in target_timeframes:
    #         df, latest_price = results[result_index]
    #         if df is not None and latest_price is not None:
    #             ohlcv_data_cache[symbol][tf] = df
    #             price_cache[symbol][tf] = latest_price
    #         result_index += 1
    
    # asyncio.gather は既にawaitされているはずなので、結果はリストで得られる
    
    # Patch 71: asyncio.gather の結果の処理を修正
    result_index = 0
    tasks_to_process = []
    # タスクリストを再構築し、 gather() を await する
    for symbol in monitoring_symbols:
        for tf in target_timeframes:
            limit = REQUIRED_OHLCV_LIMITS.get(tf, 1000)
            tasks_to_process.append(
                fetch_and_prepare_ohlcv(symbol, tf, limit=limit)
            )
    
    # 非同期実行
    # NOTE: 実際にはメインループから呼ばれるため、await asyncio.gather(...) が必要です。
    # 既存のコードを維持し、呼び出し元 (main_bot_loop) が gather を await すると仮定します。
    # ここでは、results が既に await 済みのリストであると仮定して処理を続行します。
    # 
    # 🚨 NOTE: 開発中にこの関数がawaitされずに呼ばれる可能性があるため、asyncio.gatherをこの関数内で実行します。
    
    results = await asyncio.gather(*tasks_to_process) # 内部で await 実行

    result_index = 0
    for symbol in monitoring_symbols:
        for tf in target_timeframes:
            df, latest_price = results[result_index]
            if df is not None and latest_price is not None:
                ohlcv_data_cache[symbol][tf] = df
                price_cache[symbol][tf] = latest_price
            result_index += 1
            

    # 2. 各銘柄・方向で分析を実行
    for symbol in monitoring_symbols:
        # Long/Shortの両方向をチェック
        for side in ['long', 'short']:
            
            # 必須のOHLCVデータが揃っているか確認
            if not ohlcv_data_cache[symbol]:
                continue
                
            # 既に直近で取引シグナルが出ている銘柄はスキップ (クールダウン)
            last_signal_time = LAST_SIGNAL_TIME.get(f"{symbol}-{side}", 0.0)
            if time.time() - last_signal_time < TRADE_SIGNAL_COOLDOWN:
                logging.debug(f"⏳ {symbol} - {side.capitalize()} はクールダウン中です。スキップします。")
                continue
                
            # テクニカル分析を実行
            signal_info = analyze_technical_indicators(
                ohlcv_data_cache[symbol], 
                price_cache[symbol],
                symbol, 
                side,
                macro_context
            )
            
            if signal_info and signal_info['score'] >= current_threshold:
                all_signals.append(signal_info)
                logging.info(f"✨ 高スコアシグナル検出: {symbol} - {side.capitalize()} (Score: {signal_info['score']*100:.2f}%)")
            elif signal_info:
                logging.debug(f"📉 スコア不足: {symbol} - {side.capitalize()} (Score: {signal_info['score']*100:.2f}% < {current_threshold*100:.2f}%)")


    # 3. スコアの高い順にソートし、トップN個を返す
    all_signals.sort(key=lambda x: x['score'], reverse=True)
    
    # 💡 TOP_SIGNAL_COUNT に制限 (通常は1)
    top_signals = all_signals[:TOP_SIGNAL_COUNT]
    
    return top_signals

# ====================================================================================
# TRADING EXECUTION
# ====================================================================================

async def calculate_dynamic_lot_size(signal: Dict, account_equity: float, market_info: Dict) -> Tuple[float, float, Dict]:
    """
    リスクベースのポジションサイジングに基づき、動的なロットサイズを計算する。
    
    Args:
        signal: 取引シグナル情報 (SL価格、TP価格を含む)。
        account_equity: 現在の総資産 (USDT)。
        market_info: CCXTの市場情報 (シンボル、最小取引単位など)。

    Returns:
        (lots_to_trade, risk_usdt, trade_info) のタプル。lots_to_trade は取引所の単位 (BTC/ETHなど)。
    """
    current_price = signal['current_price']
    side = signal['side']
    stop_loss = signal['stop_loss']
    
    if account_equity <= 0:
        return 0.0, 0.0, {'error_message': '口座資産がゼロまたはマイナスです。'}
    
    # 1. リスク許容額 (USDT) の計算
    risk_usdt = account_equity * MAX_RISK_PER_TRADE_PERCENT
    
    # 2. 1ロットあたりの価格変動幅 (リスク幅) を計算 (USDT)
    # リスク幅 (価格) = abs(Current_Price - Stop_Loss)
    risk_width_price = abs(current_price - stop_loss)
    
    if risk_width_price <= 0:
        return 0.0, 0.0, {'error_message': 'SL価格がエントリー価格と同じまたはゼロです。'}
        
    # 3. 取引数量 (lots) の計算
    # lots = (許容リスク額 / リスク幅) / Current_Price
    # lotsは基軸通貨単位 (BTC/ETHなど)
    
    # まず、リスク額を価格差で割って名目価値 (Notional Value) を算出 (レバレッジ考慮前)
    # notional_value_at_risk = risk_usdt / risk_width_price 
    # lots = notional_value_at_risk / current_price # (BTC/ETHの単位)
    
    # 正しい計算:
    # 許容リスク額 = lots * abs(Entry_Price - SL_Price) * (1 / Entry_Price) * Entry_Price (これは間違い)
    # 許容リスク額 = lots * abs(Entry_Price - SL_Price) # (USD相当の損失額)
    # lots (基軸通貨単位) = 許容リスク額 / abs(Entry_Price - SL_Price)
    
    lots_to_trade_raw = risk_usdt / risk_width_price
    
    # 4. 取引所のロットサイズ制約 (precision/最小名目価値) に合わせた調整
    
    # 数量の最小精度 (amount precision)
    precision = market_info.get('precision', {}).get('amount', 0.0001)
    # 最小取引単位 (最小数量 minAmount)
    min_amount = market_info.get('limits', {}).get('amount', {}).get('min', 0.0)
    # 最小名目価値 (最小取引金額 minNotional) - Patch 71 FIX の原因
    min_notional = market_info.get('limits', {}).get('cost', {}).get('min', 0.0) # 'cost'が名目価値に対応

    # 数量を精度に合わせて切り捨てる
    if precision > 0:
        # lots_to_trade_raw を精度に合わせて切り捨て
        lots_to_trade_floored = math.floor(lots_to_trade_raw / precision) * precision
    else:
        lots_to_trade_floored = lots_to_trade_raw

    # 最終的なロットサイズ
    lots_to_trade = lots_to_trade_floored
    
    # 5. 制約チェック (最小数量 & 最小名目価値)
    
    # a) 最小取引数量のチェック
    if min_amount > 0 and lots_to_trade < min_amount:
        # 最小取引数量を満たさない場合
        if lots_to_trade_raw >= min_amount:
             # 精度調整でmin_amountを下回ってしまった場合、min_amountに設定 (ただしリスクが増える可能性がある)
             lots_to_trade = min_amount
        else:
             # 生の値でも満たさない場合はスキップ
             error_msg = f"ロットサイズ ({lots_to_trade_raw:.4f} {market_info['base']}) が最小取引単位 ({min_amount:.4f}) を満たしません。"
             return 0.0, risk_usdt, {'error_message': error_msg}

    # b) 【Patch 71 FIX】最小名目価値のチェック
    notional_value = lots_to_trade * current_price
    if min_notional > 0 and notional_value < min_notional:
        # 最小名目価値を満たさない場合は取引をスキップ
        error_msg = f"名目価値 ({notional_value:.2f} USDT) が最小名目価値 ({min_notional:.2f} USDT / Code 400 の原因) を満たしません。"
        return 0.0, risk_usdt, {'error_message': error_msg}
        
        
    # 6. 最終的なロットサイズに基づく名目価値 (Notional Value)
    final_notional_value = lots_to_trade * current_price
    
    trade_info = {
        'risk_usdt': risk_usdt,
        'lots_to_trade_raw': lots_to_trade_raw,
        'lots_to_trade_final': lots_to_trade,
        'final_notional_value': final_notional_value,
        'min_amount': min_amount,
        'min_notional': min_notional,
    }
    
    return lots_to_trade, risk_usdt, trade_info


async def execute_trade_logic(signal: Dict, account_equity: float) -> Optional[Dict]:
    """
    シグナルとロットサイズに基づき、取引 (成行注文 + OCO注文) を実行する。
    """
    global EXCHANGE_CLIENT, OPEN_POSITIONS, LAST_SIGNAL_TIME
    
    symbol = signal['symbol']
    side = signal['side']
    stop_loss = signal['stop_loss']
    take_profit = signal['take_profit']
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        return {'status': 'error', 'error_message': 'CCXTクライアントが準備できていません。'}
        
    if TEST_MODE:
        return {'status': 'ok', 'filled_amount': 0.0, 'filled_usdt': 0.0, 'entry_price': signal['entry_price']}


    # 1. 市場情報 (Market Info) の取得
    market = EXCHANGE_CLIENT.markets.get(symbol)
    if not market:
        return {'status': 'error', 'error_message': f'市場情報 ({symbol}) が見つかりません。'}
        
    # 2. ロットサイズの計算 (リスクベースサイジング)
    lots_to_trade, risk_usdt, trade_info = await calculate_dynamic_lot_size(
        signal, 
        account_equity, 
        market
    )
    
    if lots_to_trade <= 0:
        # ロットサイズ計算でエラーまたは最小取引単位未満
        return {'status': 'error', 'error_message': f'ロットサイズ計算失敗: {trade_info.get("error_message", "計算エラー")}', 'risk_usdt': risk_usdt}
    
    # 数量をシグナルに追記 (通知用)
    signal['lot_size_units'] = lots_to_trade
    signal['notional_value'] = trade_info['final_notional_value']
    signal['risk_usdt'] = risk_usdt # リスク許容額
    
    # 3. メインの成行注文 (Market Order) を執行
    order_type = 'market'
    order_side = 'buy' if side == 'long' else 'sell'
    
    try:
        # ccxtはロットサイズ (lots_to_trade) を基軸通貨単位で渡す
        main_order = await EXCHANGE_CLIENT.create_order(
            symbol=symbol,
            type=order_type,
            side=order_side,
            amount=lots_to_trade,
            price=None, # 成行注文
            params={
                # MEXC の先物取引に必要なパラメータ
                'positionSide': side.upper(), # LONG/SHORT
                'leverage': LEVERAGE,
                'marginMode': 'cross' # Cross Margin
            }
        )
        
        # 注文結果の確認 (部分約定または約定失敗の可能性もあるが、ここではシンプルに)
        if main_order['status'] not in ['open', 'closed', 'ok']:
             return {'status': 'error', 'error_message': f'メイン注文失敗: 注文ステータスが予期せぬ状態です ({main_order["status"]})。'}
             
        # 約定価格と約定数量の確認
        filled_amount = main_order.get('filled', lots_to_trade) # 約定数量
        entry_price = main_order.get('average', signal['entry_price']) # 平均約定価格
        filled_usdt = entry_price * filled_amount # 名目約定額
        
        logging.info(f"✅ {symbol} - {side.capitalize()} メイン注文成功: {filled_amount:.4f} @ {entry_price:.4f} (Notional: {format_usdt(filled_usdt)} USDT)")


    except ccxt.ExchangeError as e:
        error_str = str(e)
        if '10007' in error_str or 'symbol not support api' in error_str:
            # シンボルがAPIでサポートされていない場合
            logging.error(f"❌ 取引スキップ: {symbol} はAPIでサポートされていません (Code 10007)。")
            LAST_SIGNAL_TIME[f"{symbol}-{side}"] = time.time() # クールダウンを設定
            return {'status': 'error', 'error_message': f'APIサポート外のシンボル ({symbol})。Code 10007。'}
        elif '30005' in error_str:
            # 流動性不足 (注文数量が大きすぎるか、板が薄い)
            logging.error(f"❌ 取引スキップ: {symbol} で流動性不足です (Code 30005)。")
            LAST_SIGNAL_TIME[f"{symbol}-{side}"] = time.time() # クールダウンを設定
            return {'status': 'error', 'error_message': f'流動性不足 ({symbol})。Code 30005。'}
        
        logging.error(f"❌ メイン注文失敗 - CCXTエラー ({symbol}): {e}")
        return {'status': 'error', 'error_message': f'CCXT取引エラー: {e}'}
    except Exception as e:
        logging.critical(f"❌ メイン注文失敗 - 予期せぬエラー ({symbol}): {e}", exc_info=True)
        return {'status': 'error', 'error_message': f'予期せぬエラー: {e}'}

    
    # 4. SL/TPをローカルで管理リストに追加 (後で OCO/Exit Order を実行する)
    
    # 💡 ccxtには OCO (One-Cancels-the-Other) 注文の統一インターフェースがないため、
    # SL/TPはボットのポジション監視タスク (position_monitoring_task) で管理します。
    
    # リストに追加する前に、CCXTから取得した約定価格と数量でシグナル情報を更新
    signal['entry_price'] = entry_price
    signal['filled_amount'] = filled_amount
    signal['filled_usdt'] = filled_usdt
    signal['stop_loss'] = stop_loss
    signal['take_profit'] = take_profit
    
    # 清算価格を再計算して更新
    signal['liquidation_price'] = calculate_liquidation_price(entry_price, LEVERAGE, side, MIN_MAINTENANCE_MARGIN_RATE)
    
    # ポジションリストに追加 (SL/TP監視用)
    OPEN_POSITIONS.append({
        'symbol': symbol,
        'side': side,
        'entry_price': entry_price,
        'contracts': filled_amount,
        'filled_usdt': filled_usdt,
        'stop_loss': stop_loss,
        'take_profit': take_profit,
        'liquidation_price': signal['liquidation_price'], # ローカル清算価格を保持
        'open_time': time.time(), # ポジションオープン時刻
    })
    
    # クールダウン時間を更新
    LAST_SIGNAL_TIME[f"{symbol}-{side}"] = time.time()
    
    # 成功結果を返す
    return {
        'status': 'ok', 
        'filled_amount': filled_amount, 
        'filled_usdt': filled_usdt,
        'entry_price': entry_price,
        'stop_loss': stop_loss, # OCO注文の価格
        'take_profit': take_profit, # OCO注文の価格
    }


async def close_position_logic(position: Dict, exit_type: str) -> Optional[Dict]:
    """
    指定されたポジションを決済し、結果を返す。
    """
    global EXCHANGE_CLIENT
    
    symbol = position['symbol']
    side = position['side']
    amount = position['contracts']
    entry_price = position['entry_price']
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY or amount <= 0:
        logging.error("❌ 決済失敗: クライアント未準備または数量がゼロです。")
        return None
        
    if TEST_MODE:
        logging.warning(f"⚠️ TEST_MODE: {symbol} - {side.capitalize()} ポジションの決済をスキップします。")
        # テストモードではダミーの決済結果を返す
        current_price_mock = position['entry_price'] * (1.0 + (0.01 if exit_type == 'TP' else (-0.01 if exit_type == 'SL' else 0.0)))
        pnl_usdt_mock = (current_price_mock - entry_price) * amount * (1 if side == 'long' else -1)
        
        return {
            'status': 'ok', 
            'filled_amount': amount, 
            'exit_price': current_price_mock,
            'entry_price': entry_price,
            'pnl_usdt': pnl_usdt_mock,
            'pnl_rate': (pnl_usdt_mock / (position['filled_usdt'] / LEVERAGE)) if position['filled_usdt'] > 0 else 0.0,
            'exit_type': exit_type
        }
    
    
    # 決済注文のサイドは、エントリーの逆
    close_side = 'sell' if side == 'long' else 'buy'
    
    try:
        # 成行注文で決済 (Market Order)
        close_order = await EXCHANGE_CLIENT.create_order(
            symbol=symbol,
            type='market',
            side=close_side,
            amount=amount,
            price=None, # 成行注文
            params={
                # MEXC の先物取引に必要なパラメータ
                'positionSide': side.upper(), # LONG/SHORT
            }
        )

        # 注文結果の確認
        if close_order['status'] not in ['open', 'closed', 'ok']:
             logging.error(f"❌ 決済注文失敗: 注文ステータスが予期せぬ状態です ({close_order['status']})。")
             return None
             
        # 約定価格の確認
        exit_price = close_order.get('average', 0.0)
        filled_amount = close_order.get('filled', amount)

        # 💡 PNL (損益) の計算 (CCXTのfetch_positionsを呼び出すことで最新のPNLを取得するのが確実)
        # ただし、ここでは暫定的に計算し、通知に使用します。
        
        # 損益 (USDT) を計算
        # PNL = (Exit_Price - Entry_Price) * Amount * (1 or -1)
        price_diff = exit_price - entry_price
        # long: (Exit > Entry) で利益, short: (Entry > Exit) で利益
        multiplier = 1 if side == 'long' else -1
        pnl_usdt = price_diff * filled_amount * multiplier 
        
        # PNL率の計算 (利益 / 投入証拠金)
        initial_margin = position['filled_usdt'] / LEVERAGE
        pnl_rate = pnl_usdt / initial_margin if initial_margin > 0 else 0.0
        
        logging.info(f"✅ {symbol} - {side.capitalize()} 決済成功 ({exit_type}): {filled_amount:.4f} @ {exit_price:.4f} (PNL: {format_usdt(pnl_usdt)} USDT)")

        return {
            'status': 'ok', 
            'filled_amount': filled_amount, 
            'exit_price': exit_price,
            'entry_price': entry_price,
            'pnl_usdt': pnl_usdt,
            'pnl_rate': pnl_rate,
            'exit_type': exit_type
        }

    except ccxt.ExchangeError as e:
        logging.error(f"❌ 決済注文失敗 - CCXTエラー ({symbol}): {e}")
        return None
    except Exception as e:
        logging.critical(f"❌ 決済注文失敗 - 予期せぬエラー ({symbol}): {e}", exc_info=True)
        return None


# ====================================================================================
# MAIN BOT LOGIC (Loop & Monitoring)
# ====================================================================================

async def position_monitoring_task():
    """
    ポジションを監視し、SL/TP/清算価格に達したら決済処理を行う。
    メインループとは独立して、より短い間隔で実行される。
    """
    global OPEN_POSITIONS, ACCOUNT_EQUITY_USDT
    
    while True:
        await asyncio.sleep(MONITOR_INTERVAL) # 10秒ごと
        
        if not IS_CLIENT_READY or TEST_MODE:
            continue

        # 1. 資産情報の更新 (最新のEquityとポジション情報を取得)
        account_status = await fetch_account_status()
        
        if account_status['error']:
            logging.error("❌ ポジション監視タスク: 口座ステータス取得エラー。監視をスキップします。")
            continue
            
        # CCXTから取得した最新のポジション情報
        ccxt_positions = account_status['open_positions']
        
        # 2. 現在価格の取得 (オープンポジションの銘柄のみ)
        monitor_symbols = [p['symbol'] for p in OPEN_POSITIONS]
        if not monitor_symbols:
            continue
            
        try:
            # fetch_tickersで全監視銘柄の最新価格を取得
            tickers = await EXCHANGE_CLIENT.fetch_tickers(monitor_symbols)
        except Exception as e:
            logging.error(f"❌ ポジション監視タスク: Tickers取得エラー: {e}")
            continue

        current_prices: Dict[str, float] = {}
        for symbol, ticker in tickers.items():
            if ticker and ticker.get('last') is not None:
                current_prices[symbol] = ticker['last']


        positions_to_close: List[Tuple[Dict, str]] = []
        new_open_positions: List[Dict] = []
        
        # 3. SL/TP/清算価格のチェック
        for pos in OPEN_POSITIONS:
            symbol = pos['symbol']
            side = pos['side']
            current_price = current_prices.get(symbol)
            
            if current_price is None:
                new_open_positions.append(pos) # 価格が取れない場合は監視を継続
                continue
                
            stop_loss = pos['stop_loss']
            take_profit = pos['take_profit']
            liquidation_price = pos['liquidation_price'] # ローカルで計算した清算価格
            
            trigger_type = None
            
            if side == 'long':
                # SLトリガー: 価格 <= SL or 価格 <= 清算価格
                if current_price <= stop_loss:
                    trigger_type = 'SL'
                elif current_price <= liquidation_price:
                    trigger_type = 'Liquidation'
                # TPトリガー: 価格 >= TP
                elif current_price >= take_profit:
                    trigger_type = 'TP'
            
            elif side == 'short':
                # SLトリガー: 価格 >= SL or 価格 >= 清算価格
                if current_price >= stop_loss:
                    trigger_type = 'SL'
                elif current_price >= liquidation_price:
                    trigger_type = 'Liquidation'
                # TPトリガー: 価格 <= TP
                elif current_price <= take_profit:
                    trigger_type = 'TP'

            # 4. 決済処理の実行
            if trigger_type:
                positions_to_close.append((pos, trigger_type))
            else:
                # 継続監視するポジション
                new_open_positions.append(pos)
                
        
        # 5. 決済が必要なポジションを処理
        closed_positions_count = 0
        for pos_to_close, exit_type in positions_to_close:
            
            # 【重要】CCXTから取得した最新のポジション情報と突合させる
            # CCXTのポジションリストに存在しない場合、既に取引所側で決済されている可能性がある
            is_pos_active_on_ccxt = any(
                cp['symbol'] == pos_to_close['symbol'] and cp['side'] == pos_to_close['side']
                for cp in ccxt_positions
            )
            
            if not is_pos_active_on_ccxt and exit_type != 'Liquidation':
                # 取引所側で決済済みとみなし、ローカルリストから削除するだけ (Telegram通知はスキップ)
                logging.warning(f"⚠️ {pos_to_close['symbol']} - {pos_to_close['side'].capitalize()} は既に取引所側で決済済みと判断し、ローカルリストから削除します。")
                continue # 次のポジションへ

            # 決済ロジックを実行
            trade_result = await close_position_logic(pos_to_close, exit_type)
            
            if trade_result and trade_result['status'] == 'ok':
                # Telegram通知を送信
                await send_telegram_notification(
                    format_telegram_message(
                        signal=pos_to_close, # ポジションのメタデータを渡す
                        context="ポジション決済",
                        current_threshold=get_current_threshold(GLOBAL_MACRO_CONTEXT),
                        trade_result=trade_result,
                        exit_type=exit_type
                    )
                )
                log_signal(pos_to_close, "POSITION_CLOSED", trade_result)
                closed_positions_count += 1
                
            else:
                # 決済失敗: 再度監視リストに戻す
                new_open_positions.append(pos_to_close)
                logging.error(f"❌ {pos_to_close['symbol']} の決済 ({exit_type}) に失敗しました。次回再試行します。")


        # 6. グローバルポジションリストを更新
        if closed_positions_count > 0:
            logging.info(f"🔄 ポジションリストを更新しました。{closed_positions_count} 件のポジションをクローズ。残り {len(new_open_positions)} 件。")
            OPEN_POSITIONS = new_open_positions


async def main_bot_loop():
    """BOTのメイン処理ループ。市場情報の更新、分析、取引実行を行う。"""
    global LAST_SUCCESS_TIME, LAST_ANALYSIS_SIGNALS, IS_FIRST_MAIN_LOOP_COMPLETED
    global LAST_HOURLY_NOTIFICATION_TIME, LAST_ANALYSIS_ONLY_NOTIFICATION_TIME, LAST_WEBSHARE_UPLOAD_TIME

    # 初回起動処理
    if not await initialize_exchange_client():
        logging.critical("❌ BOT起動失敗: CCXTクライアントの初期化に失敗しました。プログラムを終了します。")
        # 致命的なエラーのため、ループに入らずに終了する

    # ループ開始
    while True:
        start_time = time.time()
        
        try:
            # 1. マクロ環境の更新 (FGI)
            await calculate_fgi()
            current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT) # FGI更新後に再取得

            # 2. 口座ステータスと監視銘柄の更新
            account_status = await fetch_account_status()
            monitoring_symbols = await get_markets_and_update_symbols()
            
            # 致命的なエラーがある場合は取引を停止
            if account_status['error']:
                logging.error("❌ メインループスキップ: 口座ステータスエラーにより取引を停止します。")
                await asyncio.sleep(LOOP_INTERVAL)
                continue
                
            account_equity = account_status['total_usdt_balance']


            # 3. 初回起動完了通知 (一度だけ実行)
            if not IS_FIRST_MAIN_LOOP_COMPLETED:
                startup_message = format_startup_message(
                    account_status, 
                    GLOBAL_MACRO_CONTEXT, 
                    len(monitoring_symbols),
                    current_threshold
                )
                await send_telegram_notification(startup_message)
                IS_FIRST_MAIN_LOOP_COMPLETED = True
            

            # 4. 取引シグナル分析の実行
            top_signals = await find_top_signals(
                monitoring_symbols, 
                TARGET_TIMEFRAMES,
                GLOBAL_MACRO_CONTEXT
            )
            
            # ログ/通知用に最新の分析結果を保存
            LAST_ANALYSIS_SIGNALS = top_signals

            # 5. 取引シグナルの処理 (クールダウン、リスクチェック、実行)
            executed_trade = False
            for signal in top_signals:
                symbol = signal['symbol']
                side = signal['side']
                
                # ポジションを既に持っている場合はスキップ
                if any(p['symbol'] == symbol for p in OPEN_POSITIONS):
                    logging.warning(f"⚠️ {symbol} は既にポジションを保有しています。取引をスキップします。")
                    continue
                
                # 取引実行
                trade_result = await execute_trade_logic(signal, account_equity)
                
                # Telegram通知を送信
                await send_telegram_notification(
                    format_telegram_message(
                        signal=signal, 
                        context="取引シグナル", 
                        current_threshold=current_threshold,
                        trade_result=trade_result
                    )
                )
                log_signal(signal, "TRADE_SIGNAL", trade_result)
                
                if trade_result and trade_result['status'] == 'ok':
                    executed_trade = True
                    # 💡 成功した場合、次のループで他のシグナルをチェックする前に休憩
                    break 

            
            # 6. 分析専用通知 (1時間ごと)
            if time.time() - LAST_ANALYSIS_ONLY_NOTIFICATION_TIME > ANALYSIS_ONLY_INTERVAL and not executed_trade:
                if LAST_ANALYSIS_SIGNALS:
                    top_signal_for_notification = LAST_ANALYSIS_SIGNALS[0]
                    # スコアが閾値以上であったが、クールダウンなどの理由で取引に至らなかった場合を通知
                    if top_signal_for_notification['score'] >= current_threshold:
                        
                        notification_message = format_telegram_message(
                            signal=top_signal_for_notification, 
                            context="分析通知 (未実行)", 
                            current_threshold=current_threshold,
                            trade_result={'status': 'info', 'error_message': 'クールダウン中、ポジション保有中、またはロットサイズが不足しています。'}
                        )
                        await send_telegram_notification(notification_message)
                        
                LAST_ANALYSIS_ONLY_NOTIFICATION_TIME = time.time()


            # 7. WebShareログのアップロード (1時間ごと)
            if time.time() - LAST_WEBSHARE_UPLOAD_TIME > WEBSHARE_UPLOAD_INTERVAL:
                
                webshare_data = {
                    'timestamp_jst': datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
                    'bot_version': "v20.0.25",
                    'account_equity_usdt': ACCOUNT_EQUITY_USDT,
                    'macro_context': GLOBAL_MACRO_CONTEXT,
                    'current_threshold': current_threshold,
                    'top_signals': [s for s in LAST_ANALYSIS_SIGNALS if s['score'] >= current_threshold],
                    'open_positions': [{
                        "symbol": p['symbol'],
                        "side": p['side'],
                        "entry_price": p['entry_price'],
                        "contracts": f"{p['contracts']:.4f}",
                        "notional_value_usdt": format_usdt(p['filled_usdt']),
                        "stop_loss": format_price(p['stop_loss']),
                        "take_profit": format_price(p['take_profit']),
                    } for p in OPEN_POSITIONS],
                }
                
                await send_webshare_update(webshare_data)
                LAST_WEBSHARE_UPLOAD_TIME = time.time()


            LAST_SUCCESS_TIME = time.time()
            elapsed_time = time.time() - start_time
            logging.info(f"✅ メインループが完了しました。所要時間: {elapsed_time:.2f}秒。")
            
            # 次のループまで待機
            sleep_time = max(0, LOOP_INTERVAL - elapsed_time)
            await asyncio.sleep(sleep_time)

        except Exception as e:
            logging.critical(f"❌ メインBOTループで予期せぬ致命的なエラーが発生しました: {e}", exc_info=True)
            await send_telegram_notification(f"🚨 **致命的なBOTエラー**\nメインループでエラーが発生しました。取引が停止している可能性があります: <code>{e}</code>")
            # エラー発生時は、次のループまで待機して再試行
            await asyncio.sleep(LOOP_INTERVAL)


# ====================================================================================
# API ENDPOINTS (FastAPI)
# ====================================================================================

@app.get("/", summary="BOTの基本ステータスを取得", response_class=JSONResponse)
async def get_status():
    """現在のBOTの状態と実行状況を返す。"""
    
    status_code = 200
    if not IS_FIRST_MAIN_LOOP_COMPLETED:
        status_text = "INITIALIZING"
        status_code = 503
    elif ACCOUNT_EQUITY_USDT <= 0 and not OPEN_POSITIONS:
        status_text = "WARNING: NO FUNDS/POSITIONS"
        status_code = 200 # 警告レベル
    else:
        status_text = "RUNNING"
        
    now_jst = datetime.now(JST)
    last_success_dt = datetime.fromtimestamp(LAST_SUCCESS_TIME, tz=JST) if LAST_SUCCESS_TIME > 0 else "N/A"
    
    # マクロコンテキストからFGI情報を取得
    fgi_raw_value = GLOBAL_MACRO_CONTEXT.get('fgi_raw_value', 'N/A')
    
    # 現在の動的な閾値を取得
    current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
    
    # 監視中のポジション情報 (簡略化)
    open_positions_info = [{
        "symbol": p['symbol'],
        "side": p['side'],
        "entry_price": format_price(p['entry_price']),
        "sl": format_price(p['stop_loss']),
        "tp": format_price(p['take_profit']),
        "liq_price": format_price(p['liquidation_price']),
        "filled_usdt": format_usdt(p['filled_usdt']),
    } for p in OPEN_POSITIONS]
    
    # 最新のシグナル情報 (スコアが高いもののみ)
    top_signals_info = [{
        "symbol": s['symbol'],
        "side": s['side'],
        "timeframe": s['timeframe'],
        "score": f"{s['score']*100:.2f}%",
        "rr_ratio": f"1:{s['rr_ratio']:.2f}",
        "entry_price": format_price(s['entry_price']),
    } for s in LAST_ANALYSIS_SIGNALS[:5]]


    response_content = {
        "status": status_text,
        "version": "v20.0.25",
        "current_time_jst": now_jst.strftime("%Y-%m-%d %H:%M:%S"),
        "last_successful_run_jst": str(last_success_dt),
        "monitoring_symbols_count": len(CURRENT_MONITOR_SYMBOLS),
        "account_status": {
            "exchange": CCXT_CLIENT_NAME.upper(),
            "test_mode": TEST_MODE,
            "equity_usdt": format_usdt(ACCOUNT_EQUITY_USDT),
            "open_positions_count": len(OPEN_POSITIONS),
        },
        "macro_context": {
            "trading_threshold": f"{current_threshold*100:.2f}%",
            "fgi": fgi_raw_value,
            "fgi_proxy_score": f"{GLOBAL_MACRO_CONTEXT['fgi_proxy']*100:.2f}%",
        },
        "open_positions": open_positions_info,
        "latest_top_signals": top_signals_info,
    }
    
    return JSONResponse(content=response_content, status_code=status_code)

@app.get("/config", summary="BOT設定を取得", response_class=JSONResponse)
async def get_config():
    """BOTの主要な設定パラメータを返す。"""
    return {
        "bot_version": "v20.0.25",
        "exchange_client": CCXT_CLIENT_NAME.upper(),
        "leverage": LEVERAGE,
        "max_risk_per_trade_percent": MAX_RISK_PER_TRADE_PERCENT,
        "trade_signal_cooldown_hours": TRADE_SIGNAL_COOLDOWN / 3600,
        "base_score": BASE_SCORE,
        "dynamic_thresholds": {
            "slump": SIGNAL_THRESHOLD_SLUMP,
            "normal": SIGNAL_THRESHOLD_NORMAL,
            "active": SIGNAL_THRESHOLD_ACTIVE,
        },
        "atr_multiplier_sl": ATR_MULTIPLIER_SL,
        "min_risk_percent": MIN_RISK_PERCENT,
        "top_signal_count": TOP_SIGNAL_COUNT,
        "monitor_interval_sec": MONITOR_INTERVAL,
        "loop_interval_sec": LOOP_INTERVAL,
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
    
    # 💡 以下の形式でasyncio.runを使用することで、FastAPIと非同期タスクを統合して実行する
    
    # uvicorn Server のインスタンス化と設定
    config = uvicorn.Config(
        app, 
        host="0.0.0.0", 
        port=port, 
        log_level="info"
    )
    server = uvicorn.Server(config)

    # uvicornサーバーとメインループの終了を待つ
    loop = asyncio.get_event_loop()
    loop.run_until_complete(server.serve())
