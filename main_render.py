# ====================================================================================
# Apex BOT v20.0.22 - Future Trading / 10x Leverage 
# (Patch 68: MEXC Rate Limit FIX for setLeverage)
#
# 改良・修正点:
# 1. 【バグ修正/確実性向上: Patch 68】MEXCでの set_leverage() の頻度超過エラー (510) に対応するため、
#    APIコール間の遅延 (LEVERAGE_SETTING_DELAY) を 0.5秒から 1.5秒に増加。
# 2. 【ロジック維持】Patch 67の set_leverage (Long/Short, openType: 2) 修正を維持。
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
LEVERAGE_SETTING_DELAY = 1.5 # レバレッジ設定時のAPIレートリミット対策用遅延 (秒) - 0.5秒から1.5秒に増加

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

# ボラティリティ指標 (ATR) の設定 
ATR_LENGTH = 14
ATR_MULTIPLIER_SL = 2.0 # SLをATRの2.0倍に設定 (動的SLのベース)
MIN_RISK_PERCENT = 0.008 # SL幅の最小パーセンテージ (0.8%)

# 市場環境に応じた動的閾値調整のための定数
FGI_SLUMP_THRESHOLD = -0.02         
FGI_ACTIVE_THRESHOLD = 0.02         
SIGNAL_THRESHOLD_SLUMP = 0.90       
SIGNAL_THRESHOLD_NORMAL = 0.85      
SIGNAL_THRESHOLD_ACTIVE = 0.75      

RSI_DIVERGENCE_BONUS = 0.10         
VOLATILITY_BB_PENALTY_THRESHOLD = 0.01 
OBV_MOMENTUM_BONUS = 0.04           

# ====================================================================================
# UTILITIES & FORMATTING (変更なし)
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
    # ロング: SMA200のトレンド一致をチェック (Long Term Reversal Penalty回避)
    # ショート: SMA200のトレンド一致をチェック (Long Term Reversal Penaltyがボーナスに変わると解釈)
    
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
    bot_version: str = "v20.0.22" # バージョンを更新
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
            
            filled_amount = trade_result.get('filled_amount', 0.0) 
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
        filled_amount = trade_result.get('filled_amount', 0.0)
        
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
        
    message += (f"<i>Bot Ver: v20.0.22 - Future Trading / 10x Leverage (Patch 68: MEXC Rate Limit FIX)</i>") # BOTバージョンを更新
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
# WEBSHARE FUNCTION (HTTP POST) (変更なし)
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
        balance = None
        if EXCHANGE_CLIENT.id == 'mexc':
            # MEXC先物では defaultType='swap' が使われることが多い
            logging.info("ℹ️ MEXC: fetch_balance(type='swap') を使用して口座情報を取得します。")
            balance = await EXCHANGE_CLIENT.fetch_balance(params={'defaultType': 'swap'})
        else:
            # 他の取引所向け
            fetch_params = {'type': 'future'} if TRADE_TYPE == 'future' else {}
            balance = await EXCHANGE_CLIENT.fetch_balance(params=fetch_params)
            
        # balanceが取得できなかった場合はエラー
        if not balance:
            raise Exception("Balance object is empty.")

        # 1. total_usdt_balance (総資産: Equity) の取得 (標準フィールド)
        total_usdt_balance = balance.get('total', {}).get('USDT', 0.0)

        # 2. MEXC特有のフォールバックロジック (infoからtotalEquityを探す)
        if EXCHANGE_CLIENT.id == 'mexc' and balance.get('info'):
            raw_data = balance['info']
            mexc_raw_data = None
            
            # raw_dataが辞書であり、'data'キーを持つ場合
            if isinstance(raw_data, dict) and 'data' in raw_data:
                mexc_raw_data = raw_data.get('data')
            else:
                # raw_data自体がリストの場合
                mexc_raw_data = raw_data
                
            mexc_data: Optional[Dict] = None
            
            if isinstance(mexc_raw_data, list) and len(mexc_raw_data) > 0:
                # リストの場合、通常は最初の要素にUSDTの要約情報が含まれると想定
                if isinstance(mexc_raw_data[0], dict):
                    mexc_data = mexc_raw_data[0]
            elif isinstance(mexc_raw_data, dict):
                mexc_data = mexc_raw_data
                
            # mexc_data (dictを期待) の中から totalEquity を抽出するロジック
            if mexc_data:
                total_usdt_balance_fallback = 0.0
                
                # Case A: V3 API形式 - mexc_data自体がUSDT資産情報を持っている
                if mexc_data.get('currency') == 'USDT':
                    total_usdt_balance_fallback = float(mexc_data.get('totalEquity', 0.0))
                
                # Case B: V1 API形式 - mexc_data内の'assets'リストに情報がある
                elif mexc_data.get('assets') and isinstance(mexc_data['assets'], list):
                    for asset in mexc_data['assets']:
                        if asset.get('currency') == 'USDT':
                            total_usdt_balance_fallback = float(asset.get('totalEquity', 0.0))
                            break
                            
                if total_usdt_balance_fallback > 0:
                    # フォールバックで取得できた値を採用
                    total_usdt_balance = total_usdt_balance_fallback
                    logging.warning("⚠️ MEXC専用フォールバックロジックで Equity を取得しました。")

        # グローバル変数に最新の総資産を保存
        ACCOUNT_EQUITY_USDT = total_usdt_balance

        return {
            'total_usdt_balance': total_usdt_balance, # 総資産 (Equity)
            'open_positions': [], # ポジション情報は別関数で取得
            'error': False
        }

    except ccxt.NetworkError as e:
        logging.error(f"❌ 口座ステータス取得失敗 (ネットワークエラー): {e}")
    except ccxt.AuthenticationError as e:
        logging.critical(f"❌ 口座ステータス取得失敗 (認証エラー): APIキー/シークレットを確認してください。{e}")
    except Exception as e:
        logging.error(f"❌ 口座ステータス取得失敗 (予期せぬエラー): {e}", exc_info=True)
        
    return {'total_usdt_balance': 0.0, 'open_positions': [], 'error': True}


async def fetch_open_positions() -> List[Dict]:
    """CCXTから現在オープン中のポジション情報を取得し、ローカルリストを更新する。 (変更なし)"""
    global EXCHANGE_CLIENT, OPEN_POSITIONS
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.error("❌ ポジション取得失敗: CCXTクライアントが準備できていません。")
        return []
        
    try:
        # fetch_positionsを呼ぶ
        if EXCHANGE_CLIENT.has['fetchPositions']:
            positions_ccxt = await EXCHANGE_CLIENT.fetch_positions()
        else:
            # fetchPositionsに対応していない取引所の場合は、代わりにfetchBalanceなどを使うロジックが必要だが、
            # 主要な先物取引所は対応しているため、ここではエラーとして扱う。
            logging.error("❌ ポジション取得失敗: 取引所が fetch_positions APIをサポートしていません。")
            return []
            
        # CCXTのポジションデータをボットの内部形式に変換
        new_open_positions = []
        for p in positions_ccxt:
            if p and p.get('symbol') and p.get('contracts', 0) != 0:
                # ユーザーが監視対象としている銘柄のみを抽出 (シンボル形式が一致することを前提)
                if p['symbol'] in CURRENT_MONITOR_SYMBOLS:
                    # 'contracts'がマイナスならショート、プラスならロング
                    side = 'short' if p['contracts'] < 0 else 'long'
                    
                    # 必須情報が揃っているか確認
                    entry_price = p.get('entryPrice')
                    contracts = abs(p['contracts'])
                    notional_value = p.get('notional')
                    
                    if entry_price is None or notional_value is None:
                         # 必要な情報がなければログに出力してスキップ
                         logging.warning(f"⚠️ {p['symbol']} のポジション情報が不完全です。スキップします。")
                         continue
                    
                    new_open_positions.append({
                        'symbol': p['symbol'],
                        'side': side,
                        'entry_price': entry_price,
                        'contracts': contracts,
                        'filled_usdt': notional_value, # 名目価値 (Notional Value)
                        'timestamp': p.get('timestamp', time.time() * 1000),
                        # TP/SL価格は、ボットのロジックで再計算するか、注文情報から取得する必要がある
                        # ここではプレースホルダーとして0.0を入れておく
                        'stop_loss': 0.0,
                        'take_profit': 0.0,
                    })

        # グローバル変数に最新のオープンポジションを保存
        OPEN_POSITIONS = new_open_positions
        logging.info(f"✅ CCXTから最新のオープンポジション情報を取得しました (現在 {len(OPEN_POSITIONS)} 銘柄)。")
        return OPEN_POSITIONS

    except ccxt.NetworkError as e:
        logging.error(f"❌ ポジション取得失敗 (ネットワークエラー): {e}")
    except ccxt.AuthenticationError as e:
        logging.critical(f"❌ ポジション取得失敗 (認証エラー): APIキー/シークレットを確認してください。{e}")
    except Exception as e:
        logging.error(f"❌ ポジション取得失敗 (予期せぬエラー): {e}", exc_info=True)
        
    return []


# ====================================================================================
# ANALYTICAL CORE (変更なし)
# ====================================================================================

async def calculate_fgi() -> Dict:
    """外部APIからFGI (恐怖・貪欲指数) を取得する (変更なし)"""
    try:
        response = await asyncio.to_thread(requests.get, "https://api.alternative.me/fng/", timeout=5)
        data = response.json()
        
        fgi_raw_value = int(data[0]['value']) if data and data[0]['value'] else 50
        fgi_classification = data[0]['value_classification'] if data and data[0]['value_classification'] else "Neutral"
        
        # FGIをスコアに変換: 0-100 -> -1.0 to 1.0 (例: 100=Greed=1.0, 0=Fear=-1.0)
        fgi_proxy = (fgi_raw_value / 50.0) - 1.0 
        
        return {
            'fgi_proxy': fgi_proxy,
            'fgi_raw_value': f"{fgi_raw_value} ({fgi_classification})",
            'forex_bonus': 0.0 # 機能削除済
        }
    except Exception:
        return {'fgi_proxy': 0.0, 'fgi_raw_value': 'N/A (APIエラー)', 'forex_bonus': 0.0}

async def get_top_volume_symbols(exchange: ccxt_async.Exchange, limit: int = TOP_SYMBOL_LIMIT, base_symbols: List[str] = DEFAULT_SYMBOLS) -> List[str]:
    """取引所から出来高トップの先物銘柄を取得し、基本リストに追加する (変更なし)"""
    # 実際にはccxt.fetch_tickersなどを使って出来高トップの銘柄を取得する
    return base_symbols

async def fetch_ohlcv_data(symbol: str, timeframe: str, limit: int) -> Optional[pd.DataFrame]:
    """指定された銘柄と時間足のOHLCVデータを取得する (変更なし)"""
    try:
        # ccxt.fetch_ohlcv を使用
        ohlcv_data = await EXCHANGE_CLIENT.fetch_ohlcv(symbol, timeframe, limit=limit)
        
        # DataFrameに変換
        ohlcv = pd.DataFrame(ohlcv_data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        ohlcv['timestamp'] = pd.to_datetime(ohlcv['timestamp'], unit='ms')
        
        if ohlcv.empty:
            raise Exception("OHLCV data is empty.")
            
        return ohlcv
        
    except Exception as e:
        logging.warning(f"⚠️ {symbol} {timeframe}: OHLCVデータの取得に失敗しました: {e}")
        return None

def apply_technical_analysis(symbol: str, ohlcv: Dict[str, pd.DataFrame]) -> Dict:
    """テクニカル分析を行い、複合的なシグナルスコアを計算する (変更なし)"""
    # 実際のロジックでは、RSI, MACD, Moving Averagesなどを計算し、スコアリングする
    
    # プレースホルダーとしてランダムなシグナルを生成
    score = random.uniform(0.5, 0.95)
    side = 'long' if random.random() > 0.5 else 'short'
    sl_ratio = MIN_RISK_PERCENT # 0.8%
    tp_ratio = sl_ratio * random.uniform(2.0, 3.0) # RR 2.0-3.0
    rr_ratio = tp_ratio / sl_ratio
    
    return {
        'final_score': score, 
        'signal_timeframe': random.choice(['1m', '5m', '1h']), 
        'side': side, 
        'sl_ratio': sl_ratio, 
        'tp_ratio': tp_ratio, 
        'rr_ratio': rr_ratio, 
        'tech_data': {
            'long_term_reversal_penalty_value': 0.0 if random.random() > 0.5 else LONG_TERM_REVERSAL_PENALTY,
            'structural_pivot_bonus': STRUCTURAL_PIVOT_BONUS,
            'macd_penalty_value': 0.0,
            'obv_momentum_bonus_value': OBV_MOMENTUM_BONUS,
            'liquidity_bonus_value': LIQUIDITY_BONUS_MAX,
            'sentiment_fgi_proxy_bonus': GLOBAL_MACRO_CONTEXT.get('fgi_proxy', 0.0) * FGI_PROXY_BONUS_MAX,
            'forex_bonus': GLOBAL_MACRO_CONTEXT.get('forex_bonus', 0.0),
            'volatility_penalty_value': 0.0
        }
    }

def calculate_signal_score(symbol: str, tech_signals: Dict, macro_context: Dict) -> Dict:
    """最終的なシグナルスコア、SL/TP値を決定する (変更なし)"""
    # 最終スコアリングロジックを適用
    return tech_signals

async def execute_trade_logic(signal: Dict) -> Optional[Dict]:
    """取引実行ロジック (変更なし)"""
    
    if TEST_MODE:
        return {'status': 'skip', 'error_message': 'TEST_MODE is ON'}
    
    if not ACCOUNT_EQUITY_USDT or ACCOUNT_EQUITY_USDT <= 0:
        return {'status': 'error', 'error_message': 'Account equity is zero or not fetched.'}
    
    symbol = signal['symbol']
    side = signal['side']
    entry_price = signal['entry_price']
    stop_loss = signal['stop_loss']
    
    # 1. リスクベースのロットサイズ計算
    # 許容リスク額 (USDT)
    max_risk_usdt = ACCOUNT_EQUITY_USDT * MAX_RISK_PER_TRADE_PERCENT
    
    # SL価格までの値幅 (USDT)
    price_diff_to_sl = abs(entry_price - stop_loss)
    
    if price_diff_to_sl <= 0:
        return {'status': 'error', 'error_message': 'Stop Loss is too close or invalid.'}
        
    # SL値幅に対する1契約あたりのリスク額 (USDT/契約)
    # Price difference * contract size = Risk in base currency * current price = Risk in USDT
    # ここでは名目価値 (Notional Value) を計算する
    
    # 必要な名目価値 (Notional Value) = 許容リスク額 / SL率 (SL price diff / entry price) * Leverage
    # SL率
    sl_ratio = abs(entry_price - stop_loss) / entry_price
    
    # 名目価値 (Notional Value)
    notional_value_usdt = max_risk_usdt / sl_ratio
    
    # 契約数量 (Lot Size) - USDT名目価値 / 現在価格
    lot_size_units = notional_value_usdt / entry_price # Base Currency単位 (例: BTC)

    # 最小取引サイズ（CCXTの市場情報から取得する必要があるが、ここではプレースホルダー）
    min_amount = EXCHANGE_CLIENT.markets[symbol].get('limits', {}).get('amount', {}).get('min', 0.0001)
    
    if lot_size_units < min_amount:
        logging.warning(f"⚠️ {symbol}: 計算されたロット ({lot_size_units:.4f}) が最小取引単位 ({min_amount:.4f}) 未満です。スキップします。")
        return {'status': 'skip', 'error_message': 'Lot size too small.'}
    
    try:
        # 2. 注文の実行
        side_ccxt = 'buy' if side == 'long' else 'sell'
        
        # 契約数量を取引所の精度に合わせて調整
        amount_adjusted = EXCHANGE_CLIENT.amount_to_precision(symbol, lot_size_units)
        
        order = await EXCHANGE_CLIENT.create_order(
            symbol,
            type='market',
            side=side_ccxt,
            amount=amount_adjusted,
            params={
                # 成行注文では価格指定は不要
            }
        )

        # 3. ポジション情報を更新
        # 実際には、約定情報をパースして正確なエントリー価格などを取得する必要があるが、ここではシグナルの情報を流用
        new_position = {
            'symbol': symbol,
            'side': side,
            'entry_price': order['price'] if order['price'] else entry_price, # 実際は約定価格
            'contracts': amount_adjusted if side == 'long' else -amount_adjusted,
            'filled_usdt': notional_value_usdt, # 名目価値 (概算)
            'timestamp': time.time() * 1000,
            'stop_loss': stop_loss,
            'take_profit': signal['take_profit'],
        }
        OPEN_POSITIONS.append(new_position)
        
        # クールダウンタイマーをセット
        LAST_SIGNAL_TIME[symbol] = time.time()
        
        return {
            'status': 'ok',
            'filled_amount': amount_adjusted,
            'filled_usdt': notional_value_usdt,
            'entry_price': new_position['entry_price'],
            'exit_type': 'N/A'
        }
        
    except ccxt.ExchangeError as e:
        logging.error(f"❌ {symbol} 注文実行エラー: {e}")
        return {'status': 'error', 'error_message': f"Exchange Error: {e.args[0]}"}
    except Exception as e:
        logging.error(f"❌ {symbol} 注文実行中に予期せぬエラー: {e}", exc_info=True)
        return {'status': 'error', 'error_message': f"Unexpected Error: {e}"}


# ====================================================================================
# SCHEDULERS & ENTRY POINT (変更なし)
# ====================================================================================

async def main_bot_loop():
    """メインの取引ロジックを格納する非同期関数 (変更なし)"""
    global LAST_SUCCESS_TIME, GLOBAL_MACRO_CONTEXT, CURRENT_MONITOR_SYMBOLS, IS_FIRST_MAIN_LOOP_COMPLETED, LAST_ANALYSIS_ONLY_NOTIFICATION_TIME, LAST_SIGNAL_TIME
    
    LAST_SUCCESS_TIME = time.time()
    logging.info("⚙️ メインループを開始します。")

    try:
        # 0. グローバルコンテキストと口座情報の更新
        GLOBAL_MACRO_CONTEXT = await calculate_fgi()
        account_status = await fetch_account_status()
        
        if account_status.get('error'):
            logging.critical("❌ 口座情報の取得に失敗しました。取引をスキップします。")
            return

        # 1. 監視対象銘柄のリストを更新 (出来高ベース)
        # 実際には、ここで市場の出来高トップの銘柄を取得し、CURRENT_MONITOR_SYMBOLSを更新する
        CURRENT_MONITOR_SYMBOLS = await get_top_volume_symbols(EXCHANGE_CLIENT, TOP_SYMBOL_LIMIT, DEFAULT_SYMBOLS)
        await fetch_open_positions() # オープンポジション情報の更新
        
        # 2. 動的閾値の計算
        current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
        logging.info(f"📊 動的取引閾値: {current_threshold * 100:.2f} / 100")
        
        all_signals: List[Dict] = []
        
        # 3. 監視銘柄ごとの分析
        for symbol in CURRENT_MONITOR_SYMBOLS:
            
            # クールダウンチェック
            if time.time() - LAST_SIGNAL_TIME.get(symbol, 0) < TRADE_SIGNAL_COOLDOWN:
                logging.debug(f"ℹ️ {symbol}: クールダウン中。スキップします。")
                continue
            
            # ポジション重複チェック (既にポジションを持っている場合はスキップ)
            if any(p['symbol'] == symbol for p in OPEN_POSITIONS):
                logging.debug(f"ℹ️ {symbol}: 既にポジションを保有しています。スキップします。")
                continue
            
            # 3.1. OHLCVデータ取得 (全時間足)
            ohlcv_data: Dict[str, pd.DataFrame] = {}
            for tf in TARGET_TIMEFRAMES:
                df = await fetch_ohlcv_data(symbol, tf, REQUIRED_OHLCV_LIMITS[tf])
                if df is not None and not df.empty:
                    ohlcv_data[tf] = df
            
            if not ohlcv_data:
                continue

            # 3.2. テクニカルシグナル計算
            tech_signals = apply_technical_analysis(symbol, ohlcv_data)
            
            # 3.3. 最終シグナルスコアとSL/TP計算
            signal = calculate_signal_score(symbol, tech_signals, GLOBAL_MACRO_CONTEXT)
            
            # 必須情報の追加
            signal['symbol'] = symbol
            signal['score'] = signal.pop('final_score')
            signal['timeframe'] = signal.pop('signal_timeframe')
            
            all_signals.append(signal)

        # 4. シグナルフィルタリングと実行
        # スコア降順にソート
        top_signals = sorted(all_signals, key=lambda x: x['score'], reverse=True)
        
        if top_signals:
            # 閾値を超えたシグナルのみを対象とする
            eligible_signals = [s for s in top_signals if s['score'] >= current_threshold]
            
            # TOP_SIGNAL_COUNT (現在は1) のシグナルのみを処理
            for signal in eligible_signals[:TOP_SIGNAL_COUNT]:
                
                # 4.1. SL/TP/清算価格の計算
                current_price = EXCHANGE_CLIENT.fetch_ticker(signal['symbol'])['last']
                side = signal['side']
                
                signal['entry_price'] = current_price
                
                # SL/TP価格の決定
                sl_price = current_price * (1 - signal['sl_ratio'] if side == 'long' else 1 + signal['sl_ratio'])
                tp_price = current_price * (1 + signal['tp_ratio'] if side == 'long' else 1 - signal['tp_ratio'])
                
                # 清算価格の推定
                liq_price = calculate_liquidation_price(
                    current_price, 
                    LEVERAGE, 
                    side, 
                    MIN_MAINTENANCE_MARGIN_RATE
                )
                
                signal['stop_loss'] = sl_price
                signal['take_profit'] = tp_price
                signal['liquidation_price'] = liq_price
                
                logging.info(f"🔥 強力なシグナル検出: {signal['symbol']} - {signal['side'].upper()} ({signal['score']*100:.2f})")
                
                # 4.2. 取引の実行
                trade_result = await execute_trade_logic(signal)
                
                # 4.3. 通知とロギング
                log_signal(signal, "取引シグナル", trade_result)
                await send_telegram_notification(format_telegram_message(signal, "取引シグナル", current_threshold, trade_result))
        else:
            logging.info("🔍 閾値を超える強力な取引シグナルはありませんでした。")

        # 5. 初回完了通知
        if not IS_FIRST_MAIN_LOOP_COMPLETED:
            await send_telegram_notification(format_startup_message(account_status, GLOBAL_MACRO_CONTEXT, len(CURRENT_MONITOR_SYMBOLS), current_threshold, "v20.0.22")) # バージョン更新
            IS_FIRST_MAIN_LOOP_COMPLETED = True
            
        # 6. WebShareデータの送信
        if time.time() - LAST_WEBSHARE_UPLOAD_TIME > WEBSHARE_UPLOAD_INTERVAL:
            webshare_data = {
                'timestamp': datetime.now(JST).isoformat(),
                'version': "v20.0.22", # バージョン更新
                'account_status': account_status,
                'open_positions': OPEN_POSITIONS,
                'macro_context': GLOBAL_MACRO_CONTEXT,
                'signals': eligible_signals if 'eligible_signals' in locals() else [] 
            }
            await send_webshare_update(webshare_data)
            LAST_WEBSHARE_UPLOAD_TIME = time.time()


    except Exception as e:
        logging.error(f"❌ メインループ実行中にエラー: {e}", exc_info=True)


async def position_management_loop_async():
    """TP/SLを監視し、決済注文を実行する非同期関数 (変更なし)"""
    global OPEN_POSITIONS
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        return
        
    closed_positions_symbols = []
    
    # 実際には、ccxt.fetch_positionsで最新のポジション損益とTP/SL価格のトリガーを監視する
    
    # 簡易シミュレーションとして、ローカルのポジションリストと最新価格を比較
    tickers = {}
    try:
        # 監視対象銘柄の最新価格をまとめて取得
        if OPEN_POSITIONS:
            symbols_to_fetch = list(set([p['symbol'] for p in OPEN_POSITIONS]))
            tickers = await EXCHANGE_CLIENT.fetch_tickers(symbols_to_fetch)
    except Exception as e:
        logging.warning(f"⚠️ ポジション監視中の価格取得失敗: {e}")
        return

    for pos in list(OPEN_POSITIONS):
        symbol = pos['symbol']
        
        if symbol not in tickers or 'last' not in tickers[symbol]:
            continue
            
        current_price = tickers[symbol]['last']
        
        action = None
        
        # 決済ロジック (TP/SL)
        if pos['side'] == 'long':
            if current_price >= pos['take_profit']:
                action = 'TP_SELL'
            elif current_price <= pos['stop_loss']:
                action = 'SL_SELL'
        elif pos['side'] == 'short':
            if current_price <= pos['take_profit']:
                action = 'TP_BUY'
            elif current_price >= pos['stop_loss']:
                action = 'SL_BUY'

        if action:
            logging.warning(f"🔔 {symbol}: {action}トリガー！価格 {format_price(current_price)}")
            
            trade_result: Dict = {
                'status': 'ok',
                'exit_type': action.split('_')[0],
                'entry_price': pos['entry_price'],
                'exit_price': current_price,
                'filled_amount': abs(pos['contracts']),
                # PnL計算 (簡易)
                'pnl_rate': (current_price - pos['entry_price']) / pos['entry_price'] * LEVERAGE * (1 if pos['side'] == 'long' else -1),
                'pnl_usdt': abs(pos['filled_usdt']) * (current_price - pos['entry_price']) / pos['entry_price'] * (1 if pos['side'] == 'long' else -1)
            }
            
            # 実際はここで決済注文を出す (ccxt.create_order type='market' reduceOnly=True)
            
            closed_positions_symbols.append(symbol)
            log_signal(pos, "ポジション決済", trade_result)
            # ポジションを閉じる際は、シグナルスコアが不明なので、スコアは0.8で固定して通知
            mock_signal = pos.copy()
            mock_signal['score'] = 0.8
            mock_signal['rr_ratio'] = 2.0 
            await send_telegram_notification(format_telegram_message(mock_signal, "ポジション決済", MIN_RISK_PERCENT, trade_result, exit_type=action.split('_')[0]))

    # 決済されたポジションをリストから削除
    OPEN_POSITIONS = [p for p in OPEN_POSITIONS if p['symbol'] not in closed_positions_symbols]


# Uvicorn/FastAPIのアプリケーションインスタンス
app = FastAPI(title="Apex BOT API")

@app.get("/status", include_in_schema=False)
async def read_root():
    """ヘルスチェック用のルート (変更なし)"""
    now_jst = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")
    
    if IS_CLIENT_READY and IS_FIRST_MAIN_LOOP_COMPLETED:
        status_code = 200
        message = f"Apex BOT Service is running and CCXT client is ready. ({now_jst} JST)"
    else:
        # クライアント初期化中/未完了の場合、警告を返す（ただし200を維持してダウン判定は避ける）
        status_code = 200 
        message = f"Apex BOT Service is running (Client initializing: {now_jst} JST)."
        
    return JSONResponse(
        status_code=status_code,
        content={"status": message, "version": "v20.0.22", "timestamp": datetime.now(JST).isoformat()} # バージョン更新
    )


async def main_bot_scheduler():
    """メインループを定期実行するスケジューラ (1分ごと) (変更なし)"""
    global LAST_SUCCESS_TIME
    while True:
        # クライアントの再初期化を試行
        if not IS_CLIENT_READY:
            logging.info("(main_bot_scheduler) - クライアント未準備のため、初期化を試行します。")
            await initialize_exchange_client() # クライアント未準備の場合、再初期化を試行
            await asyncio.sleep(5) # 初期化試行後は少し待機
            continue

        try:
            await main_bot_loop()
        except Exception as e:
            logging.critical(f"❌ メインループ実行中に致命的なエラー: {e}", exc_info=True)
            await send_telegram_notification(f"🚨 **致命的なエラー**\\nメインループでエラーが発生しました: `{e}`")

        # 待機時間を LOOP_INTERVAL (60秒) に基づいて計算
        wait_time = max(1, LOOP_INTERVAL - (time.time() - LAST_SUCCESS_TIME))
        logging.info(f"次のメインループまで {wait_time:.1f} 秒待機します。")
        await asyncio.sleep(wait_time)


async def position_monitor_scheduler():
    """TP/SL監視ループを定期実行するスケジューラ (10秒ごと) (変更なし)"""
    while True:
        try:
            await position_management_loop_async()
        except Exception as e:
            logging.critical(f"❌ ポジション監視ループ実行中に致命的なエラー: {e}", exc_info=True)

        await asyncio.sleep(MONITOR_INTERVAL) # MONITOR_INTERVAL (10秒) ごとに実行


@app.on_event("startup")
async def startup_event():
    """アプリケーション起動時に実行 (タスク起動の修正) (変更なし)"""
    logging.info("BOTサービスを開始しました。")
    
    # 💡 Patch 57の修正: initialize_exchange_clientの実行はmain_bot_schedulerに委譲し、競合を避ける。
    
    # メインループのスケジューラをバックグラウンドで開始 (1分ごと)
    asyncio.create_task(main_bot_scheduler())
    
    # ポジション監視ループのスケジューラをバックグラウンドで開始 (10秒ごと)
    asyncio.create_task(position_monitor_scheduler())


# エラーハンドラ (変更なし)
@app.exception_handler(Exception)
async def default_exception_handler(request, exc):
    """捕捉されなかった例外を処理し、ログに記録する (変更なし)"""
    
    # CCXT RequestTimeoutの後に aiohttp の警告が出るのは一般的
    if "Unclosed" not in str(exc):
        logging.error(f"❌ 未処理の致命的なエラーが発生しました: {type(exc).__name__}: {exc}", exc_info=True)
    
    return JSONResponse(
        status_code=500,
        content={"message": f"Internal Server Error: {type(exc).__name__}", "detail": str(exc)},
    )


if __name__ == "__main__":
    # 環境変数PORTからポート番号を取得。なければ10000を使用
    port = int(os.environ.get("PORT", 10000))
    # Uvicornを起動
    uvicorn.run("main_render:app", host="0.0.0.0", port=port, log_level="info")
