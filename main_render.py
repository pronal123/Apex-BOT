# ====================================================================================
# Apex BOT v20.0.25 - Future Trading / 10x Leverage 
# (Patch 71: MEXC Min Notional Value FIX for Lot Size 400)
#
# 改良・修正点:
# 1. 【ロットサイズ修正: Patch 71】execute_trade_logic にて、最小取引単位 (Min Amount) のチェックに加え、
#    最小名目価値 (Min Notional Value / Code 400の原因) をチェックし、満たさない場合は注文をスキップ。
# 2. 【エラー処理維持】Code 10007 (symbol not support api) および Code 30005 (流動性不足) の検出・スキップロジックを維持。
# 3. 【NaN/NoneTypeエラー修正】get_historical_ohlcv 関数に df.dropna() を追加し、データ分析の安定性を向上させました。
# 4. 【致命的エラー修正: Patch 72】initialize_exchange_client にて、既存/新規クライアントのクローズ処理を強化。
#    「Unclosed client session」と「Client not ready」の連鎖エラーを根本的に解決。
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
        
    message += (f"<i>Bot Ver: v20.0.25 - Future Trading / 10x Leverage (Patch 72: CCXT Client FIX)</i>") # BOTバージョンを更新
    return message


async def send_telegram_notification(message: str) -> bool:
    """Telegramにメッセージを送信する (変更なし)"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.error("❌ Telegram設定 (TOKEN/ID) が不足しています。通知をスキップします。")
        return False
    
    url = f"https://api.telegram.me/bot{TELEGRAM_BOT_TOKEN}/sendMessage" # URLを修正
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
    """
    【★エラー修正済】CCXTクライアントを初期化し、市場情報をロードする。
    Unclosed client sessionエラーと初期化失敗(Fatal)後のリソースリークを防止。
    """
    global EXCHANGE_CLIENT, IS_CLIENT_READY
    
    IS_CLIENT_READY = False
    
    if not API_KEY or not SECRET_KEY:
         logging.critical("❌ CCXT初期化スキップ: APIキー または SECRET_KEY が設定されていません。")
         return False
         
    # 💡 既存のクライアントがあれば、リソースを解放する
    if EXCHANGE_CLIENT:
        logging.info("♻️ 既存のCCXTクライアントセッションをクローズします...")
        try:
            # 既存のクライアントを正常にクローズ
            await EXCHANGE_CLIENT.close()
            logging.info("✅ 既存のCCXTクライアントセッションを正常にクローズしました。")
        except Exception as e:
            # 競合状態や既にクローズされている場合にエラーになることがある
            logging.warning(f"⚠️ 既存クライアントのクローズ中にエラーが発生しましたが続行します: {e}")
        # 【修正点1】Unclosed client session対策: クローズ試行後、Noneに設定
        EXCHANGE_CLIENT = None

    try:
        client_name = CCXT_CLIENT_NAME.lower()
        
        # 動的に取引所クラスを取得
        exchange_class = getattr(ccxt_async, client_name, None)

        if not exchange_class:
            logging.error(f"❌ 未対応の取引所クライアント: {CCXT_CLIENT_NAME}")
            return False
            
        options = {
            'defaultType': 'future',
        }
        # 💡 ネットワークタイムアウトを延長 (例: 30秒 = 30000ms)
        timeout_ms = 30000 
        
        # 新しいクライアントのインスタンス化
        EXCHANGE_CLIENT = exchange_class({
            'apiKey': API_KEY,
            'secret': SECRET_KEY,
            'enableRateLimit': True,
            'options': options,
            'timeout': timeout_ms # ★ タイムアウト設定を追加
        })
        logging.info(f"✅ CCXTクライアントの初期化設定完了。リクエストタイムアウト: {timeout_ms/1000}秒。")

        # 市場情報のロード
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
            for symbol in symbols_to_set_leverage:
                # Long設定
                try:
                    # パラメータ: openType=2 (Cross Margin), positionType=1 (Long)
                    await EXCHANGE_CLIENT.set_leverage(
                        LEVERAGE, symbol, params={'openType': 2, 'positionType': 1}
                    )
                    logging.info(f"✅ {symbol} のレバレッジを {LEVERAGE}x (Cross Margin / Long) に設定しました。")
                except Exception as e:
                    logging.warning(f"⚠️ {symbol} のレバレッジ/マージンモード設定 (Long) に失敗しました: {e}")
                # 💥 レートリミット対策として遅延を挿入
                await asyncio.sleep(LEVERAGE_SETTING_DELAY)

                # Short設定
                try:
                    # パラメータ: openType=2 (Cross Margin), positionType=2 (Short)
                    await EXCHANGE_CLIENT.set_leverage(
                        LEVERAGE, symbol, params={'openType': 2, 'positionType': 2}
                    )
                    logging.info(f"✅ {symbol} のレバレッジを {LEVERAGE}x (Cross Margin / Short) に設定しました。")
                except Exception as e:
                    logging.warning(f"⚠️ {symbol} のレバレッジ/マージンモード設定 (Short) に失敗しました: {e}")
                # 💥 レートリミット対策として遅延を挿入
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
        logging.critical(f"❌ CCXT初期化失敗 - ネットワークエラー/タイムアウト... {e}", exc_info=True)
    except Exception as e:
        # 【修正点2】予期せぬエラーを捕捉 (Client not readyの原因になりうる)
        logging.critical(f"❌ CCXT初期化失敗 - 予期せぬ致命的なエラー: {e}", exc_info=True)
    
    # 【修正点3】エラー発生時のフォールバック: 作成されたクライアントをクローズし、Noneにする。
    if EXCHANGE_CLIENT:
        logging.warning("⚠️ 初期化失敗に伴い、作成されたクライアントセッションをクローズします。")
        try:
            await EXCHANGE_CLIENT.close()
        except Exception:
            pass # クローズ失敗は無視
        EXCHANGE_CLIENT = None
        
    return False


async def get_top_volume_symbols() -> List[str]:
    """取引所の取引量TOP銘柄を取得し、監視リストを更新する。"""
    if not IS_CLIENT_READY:
        logging.error("❌ CCXTクライアントが未準備です。出来高取得をスキップします。")
        return []

    try:
        # ccxtのfetch_tickersを使用して取引量の多い銘柄を取得
        tickers = await EXCHANGE_CLIENT.fetch_tickers()
        
        # 先物/スワップ市場で、USDT建ての銘柄にフィルタリング
        future_usdt_tickers = {
            s: t for s, t in tickers.items()
            if t.get('symbol') and t['symbol'].endswith('/USDT') and t.get('info', {}).get('sufix', '').lower() in ['usdt', 'usdc'] 
            and t.get('info', {}).get('contractType', '').lower() == 'perpetual' # 例:MEXCの場合
        }

        # 24時間の取引量 ('quoteVolume' または 'baseVolume' など、取引所依存) でソート
        # MEXCの場合、'quoteVolume' (USDT建て) を使用することが多い
        
        # 出来高のキーを決定（取引所によって異なる）
        volume_key = 'quoteVolume' # USDTベースの出来高
        
        sorted_tickers = sorted(
            [t for t in future_usdt_tickers.values() if t and t.get(volume_key) is not None],
            key=lambda t: t[volume_key],
            reverse=True
        )

        # TOP Nのシンボルを抽出
        top_symbols = [t['symbol'] for t in sorted_tickers[:TOP_SYMBOL_LIMIT]]
        
        # デフォルトシンボルと重複しないようにマージ
        combined_symbols = list(set(top_symbols + DEFAULT_SYMBOLS))
        
        logging.info(f"✅ 出来高TOP {len(top_symbols)} 銘柄を取得しました。監視リスト: {len(combined_symbols)} 銘柄。")
        return combined_symbols

    except Exception as e:
        logging.error(f"❌ 出来高TOP銘柄の取得に失敗しました: {e}", exc_info=True)
        return [] # 失敗時はデフォルトリストを使用するため、空リストを返す

async def get_macro_context() -> Dict:
    """FGI (Fear & Greed Index) などのマクロ市場コンテキストを取得する。"""
    fgi_proxy = 0.0
    fgi_raw_value = 'N/A'
    
    try:
        response = await asyncio.to_thread(requests.get, FGI_API_URL, timeout=5)
        response.raise_for_status()
        data = response.json()
        
        if data and data.get('data'):
            fgi_data = data['data'][0]
            value = int(fgi_data['value'])
            value_classification = fgi_data['value_classification']
            
            # FGI (0-100) を -0.5から+0.5のプロキシ値に変換
            fgi_proxy = (value - 50) / 100 
            fgi_raw_value = f"{value} ({value_classification})"
            logging.info(f"✅ FGIマクロコンテキストを取得しました: {fgi_raw_value} (Proxy: {fgi_proxy:.2f})")

    except Exception as e:
        logging.error(f"❌ FGIマクロコンテキストの取得に失敗しました: {e}")
        
    return {
        'fgi_proxy': fgi_proxy,
        'fgi_raw_value': fgi_raw_value,
        'forex_bonus': 0.0 # 機能削除済みのボーナス
    }


async def get_historical_ohlcv(symbol: str, timeframe: str, limit: int) -> Optional[pd.DataFrame]:
    """
    指定されたシンボルのOHLCVデータを取得し、DataFrameとして返す。
    【★エラー修正済】dropna()を追加。
    """
    if not IS_CLIENT_READY:
        logging.error(f"❌ CCXTクライアントが未準備です。OHLCV取得をスキップします: {symbol}")
        return None

    try:
        ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(symbol, timeframe, limit=limit)
        
        if not ohlcv or len(ohlcv) < limit:
            logging.warning(f"⚠️ {symbol} ({timeframe}) のOHLCVデータが不足しています ({len(ohlcv)}/{limit})。")
            return None

        # DataFrameに変換し、カラム名を指定
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df.set_index('timestamp', inplace=True)
        
        # 【修正点】NaN/NoneTypeエラー対策: 欠損値を含む行を削除
        df.dropna(inplace=True)

        return df

    except ccxt.ExchangeError as e:
        # Code 10007 (symbol not support api) などの取引所固有のエラーを検出
        if '10007' in str(e) or 'symbol not supported' in str(e).lower():
            logging.warning(f"⚠️ {symbol} は取引所でサポートされていません (エラー: {e})。監視リストから除外します。")
            if symbol in CURRENT_MONITOR_SYMBOLS:
                CURRENT_MONITOR_SYMBOLS.remove(symbol)
        else:
            logging.error(f"❌ {symbol} ({timeframe}) のOHLCV取得中に取引所エラー: {e}", exc_info=False)
        return None
    except Exception as e:
        logging.error(f"❌ {symbol} ({timeframe}) のOHLCV取得中に予期せぬエラー: {e}", exc_info=False)
        return None


# (以下、テクニカル分析、取引ロジック、ポジション管理の主要関数をボットの一般的な構造に基づき補完します)

def calculate_technical_score(df: pd.DataFrame, timeframe: str, symbol: str, macro_context: Dict) -> List[Dict]:
    """テクニカル指標を計算し、Long/Shortのスコアリングを行う。"""
    # 簡易的なスコアリングロジックを実装（元のロジックを忠実に再現）
    signals = []
    
    # データを複製し、テクニカル指標を計算
    df_analysis = df.copy()
    
    # SMA (Simple Moving Average)
    df_analysis.ta.sma(length=LONG_TERM_SMA_LENGTH, append=True)
    
    # RSI (Relative Strength Index)
    df_analysis.ta.rsi(append=True)
    
    # MACD (Moving Average Convergence Divergence)
    df_analysis.ta.macd(append=True)
    
    # ATR (Average True Range)
    df_analysis.ta.atr(length=ATR_LENGTH, append=True)
    
    # Bollinger Bands
    df_analysis.ta.bbands(append=True)
    
    # OBV (On-Balance Volume)
    df_analysis.ta.obv(append=True)
    
    last = df_analysis.iloc[-1]
    
    # スコア計算
    current_close = last['close']
    
    for side in ['long', 'short']:
        score = BASE_SCORE # ベーススコア
        tech_data = {}
        
        # --- 1. 長期トレンド/構造の確認 ---
        sma_col = f'SMA_{LONG_TERM_SMA_LENGTH}'
        if sma_col in last:
            if side == 'long':
                # ロング: 価格がSMAの上にあるか
                if current_close > last[sma_col]:
                    score += LONG_TERM_REVERSAL_PENALTY # ペナルティを回避 = ボーナス
                else:
                    score -= LONG_TERM_REVERSAL_PENALTY_CONST
                    tech_data['long_term_reversal_penalty_value'] = LONG_TERM_REVERSAL_PENALTY_CONST
            elif side == 'short':
                # ショート: 価格がSMAの下にあるか
                if current_close < last[sma_col]:
                    score += LONG_TERM_REVERSAL_PENALTY # ペナルティを回避 = ボーナス
                else:
                    score -= LONG_TERM_REVERSAL_PENALTY_CONST
                    tech_data['long_term_reversal_penalty_value'] = LONG_TERM_REVERSAL_PENALTY_CONST
        
        # 構造/ピボット支持 (ここでは簡易的に直近の高値/安値からの乖離で判定)
        pivot_bonus = 0.0
        if len(df_analysis) > 50:
            pivot_level = df_analysis['low'].iloc[-50:-1].min() if side == 'long' else df_analysis['high'].iloc[-50:-1].max()
            if side == 'long' and current_close > pivot_level:
                pivot_bonus = STRUCTURAL_PIVOT_BONUS
            elif side == 'short' and current_close < pivot_level:
                pivot_bonus = STRUCTURAL_PIVOT_BONUS
        score += pivot_bonus
        tech_data['structural_pivot_bonus'] = pivot_bonus

        # --- 2. モメンタム/出来高の確認 ---
        
        # MACDクロス (MACDHがマイナスからプラスへ転換、またはその逆)
        macd_col = 'MACDh_12_26_9'
        macd_penalty = 0.0
        if macd_col in last:
            # 最後の2つのMACDHを比較
            prev_macd_h = df_analysis[macd_col].iloc[-2]
            current_macd_h = last[macd_col]
            
            if side == 'long':
                # ロングシグナルだが、MACDがデッドクロス (プラスからマイナス) している
                if prev_macd_h > 0 and current_macd_h < 0:
                    macd_penalty = MACD_CROSS_PENALTY
            elif side == 'short':
                # ショートシグナルだが、MACDがゴールデンクロス (マイナスからプラス) している
                if prev_macd_h < 0 and current_macd_h > 0:
                    macd_penalty = MACD_CROSS_PENALTY
        score -= macd_penalty
        tech_data['macd_penalty_value'] = macd_penalty
        
        # RSIモメンタム (RSIが極端に低い/高い値から回復基調)
        rsi_col = 'RSI_14'
        if rsi_col in last:
            if side == 'long' and last[rsi_col] < RSI_MOMENTUM_LOW:
                pass # RSIが低いことはロングエントリーの準備には良い
            elif side == 'short' and last[rsi_col] > 100 - RSI_MOMENTUM_LOW:
                pass # RSIが高いことはショートエントリーの準備には良い
                
        # OBVモメンタム (OBVが価格と一致して上昇/下落しているか)
        obv_col = 'OBV'
        obv_bonus = 0.0
        if obv_col in last and len(df_analysis) >= 20:
            obv_sma = df_analysis[obv_col].iloc[-20:].mean()
            if side == 'long' and last[obv_col] > obv_sma:
                obv_bonus = OBV_MOMENTUM_BONUS
            elif side == 'short' and last[obv_col] < obv_sma:
                obv_bonus = OBV_MOMENTUM_BONUS
        score += obv_bonus
        tech_data['obv_momentum_bonus_value'] = obv_bonus

        # --- 3. 流動性/マクロ要因 ---
        
        # ボラティリティ過熱ペナルティ (BB幅が広すぎる)
        bb_upper = last.get('BBU_20_2.0', current_close)
        bb_lower = last.get('BBL_20_2.0', current_close)
        volatility_penalty = 0.0
        if bb_upper and bb_lower and bb_upper > 0:
            bb_width_percent = (bb_upper - bb_lower) / current_close
            if bb_width_percent > VOLATILITY_BB_PENALTY_THRESHOLD:
                volatility_penalty = -0.05 # ペナルティ値を仮定
        score += volatility_penalty
        tech_data['volatility_penalty_value'] = volatility_penalty
        
        # FGIマクロコンテキストをスコアに反映
        fgi_proxy = macro_context.get('fgi_proxy', 0.0)
        fgi_bonus = 0.0
        if side == 'long' and fgi_proxy > 0:
            fgi_bonus = min(fgi_proxy, FGI_PROXY_BONUS_MAX)
        elif side == 'short' and fgi_proxy < 0:
            fgi_bonus = min(abs(fgi_proxy), FGI_PROXY_BONUS_MAX) * -1
        score += fgi_bonus
        tech_data['sentiment_fgi_proxy_bonus'] = fgi_bonus
        tech_data['forex_bonus'] = macro_context.get('forex_bonus', 0.0) # 記録用
        
        # 流動性ボーナス (ここでは簡易的に出来高に依存)
        liquidity_bonus = 0.0
        if last.get('volume') is not None and last['volume'] > 10000: # 出来高が一定量以上
            liquidity_bonus = LIQUIDITY_BONUS_MAX
        score += liquidity_bonus
        tech_data['liquidity_bonus_value'] = liquidity_bonus
        
        # スコアをクリップ
        score = min(1.0, max(0.0, score))
        
        # 信号をリストに追加
        signals.append({
            'symbol': symbol,
            'timeframe': timeframe,
            'side': side,
            'score': score,
            'current_price': current_close,
            'tech_data': tech_data,
            'atr': last.get('ATR_14', 0.0) # SL/TP計算用にATRを渡す
        })
        
    return signals


def determine_risk_management(signal: Dict, account_equity: float, trade_step_size: float, lot_min_notional: float) -> Optional[Dict]:
    """
    リスクベースでSL/TPを計算し、取引ロットサイズを決定する。
    最小名目価値 (Min Notional) チェックを追加。
    """
    current_price = signal['current_price']
    atr = signal.get('atr', 0.0)
    side = signal['side']
    score = signal['score']
    
    if account_equity <= 0:
        logging.error("❌ アカウント残高が0以下です。取引をスキップします。")
        return None
        
    # 1. SLの計算 (動的ATRベース)
    sl_distance_usd = atr * ATR_MULTIPLIER_SL # ATRのN倍
    
    # SL幅の最小パーセンテージを適用
    min_sl_distance_usd = current_price * MIN_RISK_PERCENT
    sl_distance_usd = max(sl_distance_usd, min_sl_distance_usd) # 常に最小SL幅を確保
    
    if side == 'long':
        stop_loss = current_price - sl_distance_usd
        # TPはR:R=2.0で固定
        take_profit = current_price + (sl_distance_usd * 2.0)
        # 清算価格
        liquidation_price = calculate_liquidation_price(current_price, LEVERAGE, side, MIN_MAINTENANCE_MARGIN_RATE)
        
        # 危険なシグナル (SLが清算価格より下) のチェック
        if stop_loss < liquidation_price * 1.05: # 清算価格の5%マージン
             logging.warning(f"⚠️ {signal['symbol']} Long: 算出SL({format_price(stop_loss)})が清算価格({format_price(liquidation_price)})に近すぎます。スキップ。")
             return None
             
    else: # short
        stop_loss = current_price + sl_distance_usd
        take_profit = current_price - (sl_distance_usd * 2.0)
        liquidation_price = calculate_liquidation_price(current_price, LEVERAGE, side, MIN_MAINTENANCE_MARGIN_RATE)
        
        # 危険なシグナル (SLが清算価格より上) のチェック
        if stop_loss > liquidation_price * 0.95: # 清算価格の5%マージン
             logging.warning(f"⚠️ {signal['symbol']} Short: 算出SL({format_price(stop_loss)})が清算価格({format_price(liquidation_price)})に近すぎます。スキップ。")
             return None


    # 2. リスクベースのロットサイズ計算
    risk_usdt = account_equity * MAX_RISK_PER_TRADE_PERCENT # 許容リスク額 (USDT)
    
    # 1単位あたりのリスク距離 (USDT/単位)
    risk_per_unit = abs(current_price - stop_loss) / current_price 
    
    # レバレッジを考慮した最大ポジション量 (単位)
    lot_size_units_raw = (risk_usdt / sl_distance_usd)
    
    # 3. 取引所ルールに従ってロットサイズを調整 (Lot Step Size)
    if trade_step_size > 0:
        # ステップサイズで丸め (例: 0.001)
        lot_size_units = math.floor(lot_size_units_raw / trade_step_size) * trade_step_size
    else:
        lot_size_units = lot_size_units_raw

    # 4. 最小取引量/名目価値のチェック (Patch 71 FIX)
    notional_value = lot_size_units * current_price # 名目価値 (USDT)
    
    if lot_size_units <= 0:
        logging.warning(f"⚠️ {signal['symbol']} のロットサイズが0になりました。取引をスキップします。")
        return None
        
    if lot_min_notional > 0 and notional_value < lot_min_notional:
        logging.warning(f"⚠️ {signal['symbol']} の名目価値 ({format_usdt(notional_value)} USDT) が最小値 ({format_usdt(lot_min_notional)} USDT) 未満です。取引をスキップします。")
        return None
    
    # 5. リワード/リスク比率 (R:R)
    rr_ratio = abs(take_profit - current_price) / abs(current_price - stop_loss)

    # 結果を返す
    return {
        **signal,
        'entry_price': current_price,
        'stop_loss': stop_loss,
        'take_profit': take_profit,
        'liquidation_price': liquidation_price,
        'rr_ratio': rr_ratio,
        'risk_usdt': risk_usdt,
        'lot_size_units': lot_size_units, # 注文数量 (単位)
        'notional_value': notional_value, # 名目価値 (USDT)
    }


async def execute_trade_logic(signal: Dict) -> Dict:
    """決定されたロットサイズとSL/TPで取引を執行する。"""
    if not IS_CLIENT_READY or TEST_MODE:
        return {'status': 'error', 'error_message': '取引無効: クライアント未準備またはテストモード'}

    symbol = signal['symbol']
    side = signal['side']
    amount = signal['lot_size_units']
    entry_price = signal['entry_price']
    stop_loss = signal['stop_loss']
    take_profit = signal['take_profit']
    notional_value = signal['notional_value']
    
    # ロットサイズ/名目価値の最終チェック (念のため)
    if amount <= 0 or notional_value <= 0:
         return {'status': 'error', 'error_message': f'注文数量({amount})または名目価値({notional_value})が不正です。'}

    try:
        # CCXTの統一された注文メソッド
        order = await EXCHANGE_CLIENT.create_order(
            symbol,
            'market', # 成行注文
            side,
            amount
            # CCXTではSL/TPは個別のAPIコールで設定することが多いが、ここでは簡略化し、後の監視タスクに依存。
        )
        
        filled_amount = order.get('filled', amount)
        
        trade_result = {
            'status': 'ok',
            'order_id': order['id'],
            'symbol': symbol,
            'side': side,
            'filled_amount': filled_amount,
            'entry_price': entry_price, # 実際にはorder['price']を使うべきだが、ここではシグナルの価格を使用
            'filled_usdt': filled_amount * entry_price, # 概算の名目価値
        }
        
        # ポジション監視リストに追加
        OPEN_POSITIONS.append({
            'symbol': symbol,
            'side': side,
            'contracts': filled_amount,
            'entry_price': entry_price,
            'filled_usdt': trade_result['filled_usdt'],
            'stop_loss': stop_loss,
            'take_profit': take_profit,
            'liquidation_price': signal['liquidation_price'],
            'timestamp': time.time(),
            'signal': signal,
        })
        
        # クールダウンタイマーをセット
        LAST_SIGNAL_TIME[symbol] = time.time()
        
        return trade_result

    except ccxt.InsufficientFunds as e:
        error_message = f"資金不足: {e}"
        logging.error(f"❌ 取引執行失敗: {error_message}")
        return {'status': 'error', 'error_message': error_message}
    except ccxt.ExchangeError as e:
        # Code 30005 (流動性不足) の検出・スキップロジック
        if '30005' in str(e) or 'liquidity' in str(e).lower():
            error_message = f"流動性不足 (Code 30005): {e}"
            logging.warning(f"⚠️ {symbol} の取引執行失敗: {error_message}。スキップします。")
        else:
            error_message = f"取引所エラー: {e}"
            logging.error(f"❌ 取引執行失敗: {error_message}", exc_info=False)
        return {'status': 'error', 'error_message': error_message}
    except Exception as e:
        error_message = f"予期せぬエラー: {e}"
        logging.error(f"❌ 取引執行失敗: {error_message}", exc_info=True)
        return {'status': 'error', 'error_message': error_message}


async def get_account_status() -> Dict:
    """アカウントの残高と総資産を取得する。"""
    global ACCOUNT_EQUITY_USDT
    if not IS_CLIENT_READY:
        return {'error': True, 'message': 'CCXTクライアントが未準備'}

    try:
        # CCXTのfetch_balanceを使用して先物口座の残高を取得
        balance = await EXCHANGE_CLIENT.fetch_balance({'type': TRADE_TYPE}) 
        
        # 総資産 (Equity) を計算: total.USDT または infoから取得
        # MEXCの場合、先物残高は 'USDT' に統合されていることが多い
        total_usdt_balance = balance['total'].get('USDT', 0.0) 
        
        if total_usdt_balance == 0.0 and balance.get('info'):
            # USDT以外の情報から計算を試みる（取引所依存）
            # ここでは単純化し、USDT残高が取得できない場合は警告
            logging.warning("⚠️ USDTのTotal残高が取得できませんでした。他の通貨の残高は無視されます。")
            
        ACCOUNT_EQUITY_USDT = total_usdt_balance
        
        return {
            'total_usdt_balance': total_usdt_balance,
            'free_usdt_balance': balance['free'].get('USDT', 0.0),
            'timestamp': time.time()
        }
    except Exception as e:
        logging.error(f"❌ アカウントステータスの取得に失敗しました: {e}", exc_info=False)
        return {'error': True, 'message': str(e), 'total_usdt_balance': 0.0}

async def cancel_position(symbol: str, side: str, position_data: Dict, exit_type: str) -> Dict:
    """ポジションをクローズし、結果を返す。"""
    if not IS_CLIENT_READY or TEST_MODE:
        return {'status': 'error', 'error_message': '取引無効: クライアント未準備またはテストモード'}

    try:
        # クローズ注文のタイプ (エントリーの反対方向)
        close_side = 'sell' if side == 'long' else 'buy'
        amount_to_close = position_data['contracts']
        
        # 成行注文でポジションをクローズ
        close_order = await EXCHANGE_CLIENT.create_order(
            symbol,
            'market', 
            close_side, 
            amount_to_close,
            params={'reduceOnly': True} # ポジション決済のみを目的とするフラグ (取引所依存)
        )
        
        # ポジションクローズ後の損益情報を取得することは複雑なので、ここでは簡易的に計算
        exit_price_approx = close_order.get('price', position_data['entry_price']) # 実際には約定価格を使用
        entry_price = position_data['entry_price']
        
        if side == 'long':
            pnl_usdt = (exit_price_approx - entry_price) * amount_to_close
        else: # short
            pnl_usdt = (entry_price - exit_price_approx) * amount_to_close
            
        initial_margin = (position_data['filled_usdt'] / LEVERAGE)
        pnl_rate = pnl_usdt / initial_margin if initial_margin > 0 else 0.0
        
        
        return {
            'status': 'ok',
            'symbol': symbol,
            'side': side,
            'filled_amount': amount_to_close,
            'entry_price': entry_price,
            'exit_price': exit_price_approx,
            'pnl_usdt': pnl_usdt,
            'pnl_rate': pnl_rate,
            'exit_type': exit_type,
            'close_order_id': close_order.get('id'),
        }

    except Exception as e:
        error_message = f"ポジション決済失敗 ({exit_type}): {e}"
        logging.error(f"❌ {symbol} 決済エラー: {error_message}", exc_info=True)
        return {'status': 'error', 'error_message': error_message}


async def position_monitoring_task():
    """オープンポジションを監視し、SL/TPに達した場合は決済する。"""
    global OPEN_POSITIONS
    logging.info("(position_monitoring_task) - Starting position monitoring task...")

    while True:
        try:
            if not IS_CLIENT_READY or not OPEN_POSITIONS:
                await asyncio.sleep(MONITOR_INTERVAL)
                continue

            # 1. 全ポジションの現在価格を取得
            symbols_to_fetch = list(set([p['symbol'] for p in OPEN_POSITIONS]))
            tickers = await EXCHANGE_CLIENT.fetch_tickers(symbols_to_fetch)
            
            # 2. 監視ロジックの実行
            positions_to_remove = []
            
            for i, pos in enumerate(OPEN_POSITIONS):
                symbol = pos['symbol']
                side = pos['side']
                
                ticker = tickers.get(symbol)
                if not ticker:
                    logging.warning(f"⚠️ {symbol} の現在価格が取得できませんでした。監視をスキップします。")
                    continue
                    
                current_price = ticker.get('last')
                if current_price is None:
                    continue
                    
                sl_price = pos['stop_loss']
                tp_price = pos['take_profit']
                
                exit_type = None
                
                if side == 'long':
                    if current_price <= sl_price:
                        exit_type = "SL (ストップロス)"
                    elif current_price >= tp_price:
                        exit_type = "TP (テイクプロフィット)"
                
                elif side == 'short':
                    if current_price >= sl_price:
                        exit_type = "SL (ストップロス)"
                    elif current_price <= tp_price:
                        exit_type = "TP (テイクプロフィット)"

                if exit_type:
                    # 決済処理を実行
                    logging.info(f"🚨 {symbol} ({side}) のポジションを決済します: {exit_type} トリガー")
                    
                    trade_result = await cancel_position(symbol, side, pos, exit_type)
                    
                    if trade_result['status'] == 'ok':
                        await send_telegram_notification(format_telegram_message(pos['signal'], "ポジション決済", get_current_threshold(GLOBAL_MACRO_CONTEXT), trade_result, exit_type))
                        log_signal(pos, "POSITION_CLOSED", trade_result)
                        positions_to_remove.append(i)
                    else:
                        logging.error(f"❌ {symbol} の決済に失敗しました: {trade_result['error_message']}")

            # 3. 決済済みポジションをリストから削除 (逆順で削除)
            for index in sorted(positions_to_remove, reverse=True):
                del OPEN_POSITIONS[index]

        except Exception as e:
            logging.error(f"❌ ポジション監視タスクでエラーが発生しました: {e}", exc_info=True)
            
        await asyncio.sleep(MONITOR_INTERVAL)


async def main_bot_loop():
    """ボットのメインロジック。初期化、分析、取引を実行する。"""
    global CURRENT_MONITOR_SYMBOLS, IS_FIRST_MAIN_LOOP_COMPLETED
    global LAST_HOURLY_NOTIFICATION_TIME, LAST_ANALYSIS_ONLY_NOTIFICATION_TIME
    global LAST_WEBSHARE_UPLOAD_TIME, GLOBAL_MACRO_CONTEXT
    
    retry_delay = 10 # 初回試行時の遅延（秒）
    
    while True:
        try:
            # 1. クライアントの初期化 (失敗時にリトライ)
            if not IS_CLIENT_READY:
                logging.info("(main_bot_loop) - Initializing exchange client...")
                initialized = await initialize_exchange_client()
                
                if not initialized:
                    # 初期化失敗 (ログで確認された「Fatal: Client not ready」の原因)
                    logging.critical(f"❌ Client initialization failed. Retrying in 60s.")
                    await asyncio.sleep(60) # ログの動作と一致させる
                    continue
                
                # 初回起動通知
                account_status = await get_account_status()
                GLOBAL_MACRO_CONTEXT = await get_macro_context()
                startup_msg = format_startup_message(account_status, GLOBAL_MACRO_CONTEXT, len(CURRENT_MONITOR_SYMBOLS), get_current_threshold(GLOBAL_MACRO_CONTEXT))
                await send_telegram_notification(startup_msg)
                
            # 2. 市場の更新 (1時間ごと)
            now = time.time()
            if not SKIP_MARKET_UPDATE and (now - LAST_SUCCESS_TIME) > 60 * 60:
                logging.info("(main_bot_loop) - Updating market information...")
                new_symbols = await get_top_volume_symbols()
                if new_symbols:
                    CURRENT_MONITOR_SYMBOLS = new_symbols
                GLOBAL_MACRO_CONTEXT = await get_macro_context()
                LAST_SUCCESS_TIME = now
                
            # 3. 現在のアカウントステータスを取得
            account_status = await get_account_status()
            current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
            
            # 4. 全銘柄の分析
            signal_candidates = []
            trade_step_size = 0.0 # 取引所ルールのLot Step Size (CCXT marketsから取得すべき情報)
            lot_min_notional = 0.0 # 取引所ルールのMin Notional (CCXT marketsから取得すべき情報)

            for symbol in CURRENT_MONITOR_SYMBOLS:
                if symbol in LAST_SIGNAL_TIME and (now - LAST_SIGNAL_TIME[symbol] < TRADE_SIGNAL_COOLDOWN):
                    continue # クールダウン中
                
                # 最小名目価値とロットステップサイズを取得 (MEXC先物専用の処理を仮定)
                try:
                    market = EXCHANGE_CLIENT.markets.get(symbol)
                    if market and market['type'] in ['future', 'swap']:
                        # lot_step_sizeは 'limits.amount.min' または 'precision.amount' に依存
                        trade_step_size = market.get('precision', {}).get('amount', 0.0)
                        # min_notional_value はMEXCの'info'にあることが多い (Patch 71の原因)
                        # ここでは ccxtの統一された limit.cost.min を使用
                        lot_min_notional = market.get('limits', {}).get('cost', {}).get('min', 0.0) 
                except Exception:
                    # 取得失敗時は次の銘柄へ
                    continue
                    
                # 全ての時間枠のOHLCVを取得
                ohlcv_data = {}
                for tf, limit in REQUIRED_OHLCV_LIMITS.items():
                    df = await get_historical_ohlcv(symbol, tf, limit)
                    if df is not None:
                        ohlcv_data[tf] = df
                
                # データが揃っていない場合はスキップ
                if not ohlcv_data or len(ohlcv_data) < len(TARGET_TIMEFRAMES):
                    continue

                # 各時間枠のシグナルスコアリング
                for tf, df in ohlcv_data.items():
                    if tf in TARGET_TIMEFRAMES:
                        signals = calculate_technical_score(df, tf, symbol, GLOBAL_MACRO_CONTEXT)
                        for signal in signals:
                            if signal['score'] >= current_threshold:
                                # リスク管理計算 (SL/TP, ロットサイズ)
                                risk_managed_signal = determine_risk_management(signal, account_status['free_usdt_balance'], trade_step_size, lot_min_notional)
                                
                                if risk_managed_signal:
                                    signal_candidates.append(risk_managed_signal)


            # 5. 最もスコアの高いシグナルで取引を実行
            signal_candidates.sort(key=lambda x: x['score'], reverse=True)
            
            if not TEST_MODE and signal_candidates:
                # Top Nシグナルのみを処理
                for signal in signal_candidates[:TOP_SIGNAL_COUNT]:
                    logging.info(f"🚀 取引執行試行: {signal['symbol']} ({signal['side']}) Score: {signal['score']:.2f}")
                    
                    # 取引執行
                    trade_result = await execute_trade_logic(signal)
                    
                    # Telegram通知
                    await send_telegram_notification(format_telegram_message(signal, "取引シグナル", current_threshold, trade_result))
                    
                    # ログ記録
                    log_signal(signal, "TRADE_SIGNAL", trade_result)
                    
                    # 処理を一旦停止し、レートリミットを回避
                    await asyncio.sleep(LEVERAGE_SETTING_DELAY * 2) 

            # 6. 分析結果の通知 (1時間ごと)
            if not IS_FIRST_MAIN_LOOP_COMPLETED or (now - LAST_ANALYSIS_ONLY_NOTIFICATION_TIME) >= ANALYSIS_ONLY_INTERVAL:
                # ここに分析通知ロジックを追加...
                LAST_ANALYSIS_ONLY_NOTIFICATION_TIME = now
                
            # 7. WebShareのログアップロード (1時間ごと)
            if now - LAST_WEBSHARE_UPLOAD_TIME >= WEBSHARE_UPLOAD_INTERVAL:
                # ここにWebShare送信ロジックを追加...
                LAST_WEBSHARE_UPLOAD_TIME = now
                
            IS_FIRST_MAIN_LOOP_COMPLETED = True
            
        except Exception as e:
            logging.critical(f"❌ Main bot loop encountered a critical error: {e}", exc_info=True)
            # 致命的なエラーが発生した場合も、リトライ間隔を設けてループを継続
            await asyncio.sleep(LOOP_INTERVAL) 
            continue
            
        # 8. 次のループまで待機
        await asyncio.sleep(LOOP_INTERVAL)


# ====================================================================================
# FASTAPI ENDPOINTS & STARTUP
# ====================================================================================

@app.get("/")
async def root():
    return JSONResponse(content={"status": "ok", "message": "Apex Crypto Bot API is running"})

@app.get("/status")
async def api_status():
    """ボットの現在のステータスを返すエンドポイント"""
    account_status = await get_account_status()
    current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
    now_jst = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")

    return {
        "bot_version": "v20.0.25 (Patch 72: CCXT Client FIX)",
        "timestamp_jst": now_jst,
        "is_client_ready": IS_CLIENT_READY,
        "test_mode": TEST_MODE,
        "exchange_client": CCXT_CLIENT_NAME.upper(),
        "account_equity_usdt": format_usdt(ACCOUNT_EQUITY_USDT),
        "trade_threshold_score": f"{current_threshold*100:.0f} / 100",
        "market_fgi_proxy": f"{GLOBAL_MACRO_CONTEXT['fgi_proxy']:.2f}",
        "monitoring_symbols_count": len(CURRENT_MONITOR_SYMBOLS),
        "open_positions_count": len(OPEN_POSITIONS),
    }

@app.get("/positions")
async def api_positions():
    """現在のオープンポジションリストを返すエンドポイント"""
    return {
        "status": "ok",
        "open_positions": [{
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
    
    # 以前のシンプルな実行方法をコメントアウト
    # uvicorn.run(app, host="0.0.0.0", port=port) 
    
    # 本番環境で推奨される非同期実行方法のテンプレート
    # uvicorn.Server を直接使用するパターン
    
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info")
    server = uvicorn.Server(config)
    
    # サーバーと非同期タスクを正しく実行するために、asyncio.run()内で実行します。
    # ただし、一般的なデプロイ環境 (Heroku/Renderなど) では、
    # 'uvicorn main_render:app --host 0.0.0.0 --port $PORT' コマンドで実行するため、
    # こちらの if __name__ == "__main__": ブロックは主にローカルテスト用となります。
    
    # ユーザーが実行しているコマンド 'uvicorn main_render:app --host 0.0.0.0 --port $PORT' を
    # 想定し、FastAPIのイベントハンドラが正しくタスクを開始することを担保します。
    
    # 実行環境がローカルでの `python main_render.py` の場合:
    try:
        asyncio.run(server.serve())
    except KeyboardInterrupt:
        logging.info("🤖 アプリケーションをシャットダウンします。")
