# ====================================================================================
# Apex BOT v20.0.43 - Future Trading / 30x Leverage 
# (Feature: 実践的スコアリングロジック、ATR動的リスク管理導入)
# 
# 🚨 致命的エラー修正強化: 
# 1. fetch_tickersのAttributeError ('NoneType' object has no attribute 'keys') 対策 
# 2. 注文失敗エラー (Amount can not be less than zero) 対策
# 3. 💡 修正: 通知メッセージでEntry/SL/TP/清算価格が0になる問題を解決 (v20.0.42で対応済み)
# 4. 💡 新規: ダミーロジックを実践的スコアリングロジックに置換 (v20.0.43)
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
from fastapi import FastAPI, Request, Response 
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
    level=logging.INFO, 
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
TOP_SYMBOL_LIMIT = 40               
BOT_VERSION = "v20.0.43"            # 💡 BOTバージョンを更新 
FGI_API_URL = "https://api.alternative.me/fng/?limit=1" 

LOOP_INTERVAL = 60 * 1              
ANALYSIS_ONLY_INTERVAL = 60 * 5    
WEBSHARE_UPLOAD_INTERVAL = 60 * 60  
MONITOR_INTERVAL = 10               

# 💡 クライアント設定
CCXT_CLIENT_NAME = os.getenv("EXCHANGE_CLIENT", "mexc")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
API_KEY = os.getenv(f"{CCXT_CLIENT_NAME.upper()}_API_KEY")
SECRET_KEY = os.getenv(f"{CCXT_CLIENT_NAME.upper()}_SECRET")
TEST_MODE = os.getenv("TEST_MODE", "False").lower() in ('true', '1', 't')
SKIP_MARKET_UPDATE = os.getenv("SKIP_MARKET_UPDATE", "False").lower() in ('true', '1', 't')

# 💡 先物取引設定 
LEVERAGE = 30 
TRADE_TYPE = 'future' 
MIN_MAINTENANCE_MARGIN_RATE = 0.005 

# 💡 レートリミット対策用定数
LEVERAGE_SETTING_DELAY = 1.0 

# 💡 【固定ロット】設定 
FIXED_NOTIONAL_USDT = 20.0 

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
OPEN_POSITIONS: List[Dict] = [] 
ACCOUNT_EQUITY_USDT: float = 0.0 

if TEST_MODE:
    logging.warning("⚠️ WARNING: TEST_MODE is active. Trading is disabled.")

# CCXTクライアントの準備完了フラグ
IS_CLIENT_READY: bool = False

# 取引ルール設定
TRADE_SIGNAL_COOLDOWN = 60 * 60 * 12 
SIGNAL_THRESHOLD = 0.65             
TOP_SIGNAL_COUNT = 1                
REQUIRED_OHLCV_LIMITS = {'1m': 1000, '5m': 1000, '15m': 1000, '1h': 1000, '4h': 1000} 

# テクニカル分析定数 
TARGET_TIMEFRAMES = ['1m', '5m', '15m', '1h', '4h'] 
BASE_SCORE = 0.40                  
LONG_TERM_SMA_LENGTH = 200         
LONG_TERM_REVERSAL_PENALTY = 0.20   # 順張りボーナス/逆張りペナルティの値
STRUCTURAL_PIVOT_BONUS = 0.05       
RSI_MOMENTUM_LOW = 40              # RSI 40/60をモメンタム加速の閾値に使用
MACD_CROSS_PENALTY = 0.15          # MACDの方向一致ボーナス/不一致ペナルティ
LIQUIDITY_BONUS_MAX = 0.06          
FGI_PROXY_BONUS_MAX = 0.05         
FOREX_BONUS_MAX = 0.0               

# ボラティリティ指標 (ATR) の設定 
ATR_LENGTH = 14
ATR_MULTIPLIER_SL = 2.0             # SL = 2.0 * ATR
MIN_RISK_PERCENT = 0.008            # 最低リスク幅 (0.8%) 
RR_RATIO_TARGET = 1.5               # 💡 新規追加: 基本的なリスクリワード比率 (1:1.5)

# 市場環境に応じた動的閾値調整のための定数
FGI_SLUMP_THRESHOLD = -0.02         
FGI_ACTIVE_THRESHOLD = 0.02         
SIGNAL_THRESHOLD_SLUMP = 0.90       
SIGNAL_THRESHOLD_NORMAL = 0.85      
SIGNAL_THRESHOLD_ACTIVE = 0.80      

RSI_DIVERGENCE_BONUS = 0.10         
VOLATILITY_BB_PENALTY_THRESHOLD = 0.01 # BB幅が価格の1%を超えるとペナルティ
OBV_MOMENTUM_BONUS = 0.04           

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
        
    return max(0.0, liquidation_price) 

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

def format_startup_message(account_status: Dict, macro_context: Dict, monitoring_count: int, current_threshold: float) -> str:
    """BOT起動完了時の通知メッセージを整形する。"""
    
    now_jst = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")
    bot_version = BOT_VERSION
    
    fgi_proxy = macro_context.get('fgi_proxy', 0.0)
    fgi_raw_value = macro_context.get('fgi_raw_value', 'N/A')
    forex_bonus = macro_context.get('forex_bonus', 0.0)
    
    # current_threshold に応じてテキストを決定するロジック
    if current_threshold == SIGNAL_THRESHOLD_SLUMP:
        market_condition_text = "低迷/リスクオフ"
    elif current_threshold == SIGNAL_THRESHOLD_ACTIVE:
        market_condition_text = "活発/リスクオン"
    else:
        market_condition_text = "通常/中立"
        
    trade_status = "自動売買 **ON** (Long/Short)" if not TEST_MODE else "自動売買 **OFF** (TEST_MODE)"

    header = (
        f"🤖 **Apex BOT 起動完了通知** 🟢\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - **確認日時**: {now_jst} (JST)\n"
        f"  - **取引所**: <code>{CCXT_CLIENT_NAME.upper()}</code> (先物モード / **{LEVERAGE}x**)\n" 
        f"  - **自動売買**: <b>{trade_status}</b>\n"
        f"  - **取引ロット**: **固定** <code>{format_usdt(FIXED_NOTIONAL_USDT)}</code> **USDT**\n" 
        f"  - **最大リスク/取引**: **ATRベース**の**動的SL**で管理\n" 
        f"  - **監視銘柄数**: <code>{monitoring_count}</code>\n"
        f"  - **BOTバージョン**: <code>{bot_version}</code>\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n\n"
    )

    balance_section = f"💰 <b>先物口座ステータス</b>\n" 
    if account_status.get('error'):
        balance_section += f"<pre>⚠️ ステータス取得失敗 (致命的エラーにより取引停止中)</pre>\n"
    else:
        equity_display = account_status['total_usdt_balance'] 
        balance_section += (
            f"  - **総資産 (Equity)**: <code>{format_usdt(equity_display)}</code> USDT\n" 
        )
        
        # ボットが管理しているポジション
        if OPEN_POSITIONS:
            total_managed_value = sum(p['filled_usdt'] for p in OPEN_POSITIONS) 
            balance_section += (
                f"  - **管理中ポジション**: <code>{len(OPEN_POSITIONS)}</code> 銘柄 (名目価値合計: <code>{format_usdt(total_managed_value)}</code> USDT)\n" 
            )
            for i, pos in enumerate(OPEN_POSITIONS[:3]): # Top 3のみ表示
                base_currency = pos['symbol'].split('/')[0] # /USDTを除去
                side_tag = '🟢L' if pos.get('side', 'long') == 'long' else '🔴S' 
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
        f"  - **FGI (恐怖・貪欲)**: <code>{GLOBAL_MACRO_CONTEXT.get('fgi_raw_value', 'N/A')}</code> ({'リスクオン' if fgi_proxy > FGI_ACTIVE_THRESHOLD else ('リスクオフ' if fgi_proxy < FGI_SLUMP_THRESHOLD else '中立')})\n"
        f"  - **総合マクロ影響**: <code>{((fgi_proxy + forex_bonus) * 100):.2f}</code> 点\n\n"
    )

    footer = (
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"<pre>※ この通知はメインの分析ループが一度完了したことを示します。約1分ごとに分析が実行されます。</pre>"
    )

    return header + balance_section + macro_section + footer


def format_telegram_message(signal: Dict, context: str, current_threshold: float, trade_result: Optional[Dict] = None, exit_type: Optional[str] = None) -> str:
    now_jst = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")
    symbol = signal['symbol']
    timeframe = signal.get('timeframe', '1h')
    score = signal['score']
    side = signal.get('side', 'long') 
    
    # 価格情報、RR比率は signal 辞書または trade_result から取得 (process_entry_signalで更新されているはず)
    entry_price = signal.get('entry_price', 0.0)
    stop_loss = signal.get('stop_loss', 0.0)
    take_profit = signal.get('take_profit', 0.0)
    liquidation_price = signal.get('liquidation_price', 0.0) 
    rr_ratio = signal.get('rr_ratio', 0.0)
    
    estimated_wr = get_estimated_win_rate(score)
    
    breakdown_details = get_score_breakdown(signal) if context != "ポジション決済" else ""

    trade_section = ""
    trade_status_line = ""
    
    # リスク幅、リワード幅の計算 (0でないことを前提とする)
    risk_width = abs(entry_price - stop_loss)
    reward_width = abs(take_profit - entry_price)
    
    # SL比率 (EntryPriceに対するリスク幅)
    sl_ratio = risk_width / entry_price if entry_price > 0 else 0.0


    if context == "取引シグナル":
        
        notional_value = trade_result.get('filled_usdt', FIXED_NOTIONAL_USDT) if trade_result else FIXED_NOTIONAL_USDT # 実際に約定した名目価値
        
        trade_type_text = "先物ロング" if side == 'long' else "先物ショート"
        order_type_text = "成行買い" if side == 'long' else "成行売り"
        
        if TEST_MODE:
            trade_status_line = f"⚠️ **テストモード**: 取引は実行されません。(ロット: {format_usdt(notional_value)} USDT, {LEVERAGE}x)" 
        elif trade_result is None or trade_result.get('status') == 'error':
            trade_status_line = f"❌ **自動売買 失敗**: {trade_result.get('error_message', 'APIエラー')}"
        elif trade_result.get('status') == 'ok':
            trade_status_line = f"✅ **自動売買 成功**: **{trade_type_text}**注文を執行しました。" 
            
            filled_amount_raw = trade_result.get('filled_amount', 0.0)
            try:
                filled_amount = float(filled_amount_raw)
            except (ValueError, TypeError):
                filled_amount = 0.0
                
            filled_usdt_notional = trade_result.get('filled_usdt', FIXED_NOTIONAL_USDT) 
            # 💡 修正: リスク額はSL比率とレバレッジで計算
            risk_usdt = abs(filled_usdt_notional * sl_ratio * LEVERAGE) 
            
            trade_section = (
                f"💰 **取引実行結果**\n"
                f"  - **注文タイプ**: <code>先物 (Future) / {order_type_text} ({side.capitalize()})</code>\n" 
                f"  - **レバレッジ**: <code>{LEVERAGE}</code> 倍\n" 
                f"  - **名目ロット**: <code>{format_usdt(filled_usdt_notional)}</code> USDT (固定)\n" 
                f"  - **推定リスク額**: <code>{format_usdt(risk_usdt)}</code> USDT (計算 SL: {sl_ratio*100:.2f}%)\n"
                f"  - **約定数量**: <code>{filled_amount:.4f}</code> {symbol.split('/')[0]}\n"
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
        try:
            filled_amount = float(filled_amount_raw)
            # ポジションをクローズするため、契約数は元のポジションの契約数を反映させるべき
            if 'contracts' in signal:
                 filled_amount = signal['contracts'] 
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
        f"  - **取引閾値**: <code>{current_threshold * 100:.0f}</code> 点\n"
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
            f"{get_score_breakdown(signal)}\n"
            f"  <code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        )
        
    message += (f"<i>Bot Ver: {BOT_VERSION} - Future Trading / {LEVERAGE}x Leverage</i>") 
    return message


async def send_telegram_notification(message: str) -> bool:
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

def get_score_breakdown(signal: Dict) -> str:
    """シグナルのスコア内訳を整形して返す"""
    tech_data = signal.get('tech_data', {})
    
    breakdown_list = []
    
    # トレンド一致/逆行
    trend_val = tech_data.get('long_term_reversal_penalty_value', 0.0)
    trend_text = "🟢 長期トレンド一致" if trend_val > 0 else ("🔴 長期トレンド逆行" if trend_val < 0 else "🟡 長期トレンド中立")
    breakdown_list.append(f"{trend_text}: {trend_val*100:+.2f} 点")

    # MACDモメンタム
    macd_val = tech_data.get('macd_penalty_value', 0.0)
    macd_text = "🟢 MACDモメンタム一致" if macd_val > 0 else ("🔴 MACDモメンタム逆行/失速" if macd_val < 0 else "🟡 MACD中立")
    breakdown_list.append(f"{macd_text}: {macd_val*100:+.2f} 点")
    
    # RSIモメンタム加速
    rsi_val = tech_data.get('rsi_momentum_bonus_value', 0.0)
    if rsi_val > 0:
        breakdown_list.append(f"🟢 RSIモメンタム加速/適正水準: {rsi_val*100:+.2f} 点")
    
    # OBV確証
    obv_val = tech_data.get('obv_momentum_bonus_value', 0.0)
    if obv_val > 0:
        breakdown_list.append(f"🟢 OBV出来高確証: {obv_val*100:+.2f} 点")

    # 流動性ボーナス
    liq_val = tech_data.get('liquidity_bonus_value', 0.0)
    if liq_val > 0:
        breakdown_list.append(f"🟢 流動性 (TOP銘柄): {liq_val*100:+.2f} 点")
        
    # FGIマクロ影響
    fgi_val = tech_data.get('sentiment_fgi_proxy_bonus', 0.0)
    fgi_text = "🟢 FGIマクロ追い風" if fgi_val > 0 else ("🔴 FGIマクロ向かい風" if fgi_val < 0 else "🟡 FGIマクロ中立")
    breakdown_list.append(f"{fgi_text}: {fgi_val*100:+.2f} 点")
    
    # ボラティリティペナルティ
    vol_val = tech_data.get('volatility_penalty_value', 0.0)
    if vol_val < 0:
        breakdown_list.append(f"🔴 ボラティリティ過熱ペナルティ: {vol_val*100:+.2f} 点")
        
    # 構造的ボーナス
    struct_val = tech_data.get('structural_pivot_bonus', 0.0)
    breakdown_list.append(f"🟢 構造的優位性 (ベース): {struct_val*100:+.2f} 点")
    
    return "\n".join([f"    - {line}" for line in breakdown_list])

def _to_json_compatible(obj):
    """
    再帰的にオブジェクトをJSON互換の型に変換するヘルパー関数。
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
    try:
        log_entry = {
            'timestamp_jst': datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
            'log_type': log_type,
            'symbol': data.get('symbol', 'N/A'),
            'side': data.get('side', 'N/A'), 
            'timeframe': data.get('timeframe', 'N/A'),
            'score': data.get('score', 0.0),
            'rr_ratio': data.get('rr_ratio', 0.0),
            # 💡 修正: ログに記録する際、Entry/SL/TPも記録
            'entry_price': data.get('entry_price', 0.0),
            'stop_loss': data.get('stop_loss', 0.0),
            'take_profit': data.get('take_profit', 0.0),
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

# 🆕 機能追加: 取引閾値未満の最高スコアを定期通知
async def notify_highest_analysis_score():
    """
    分析の結果、取引閾値に満たない最高スコア銘柄を1時間ごとに通知する。
    """
    global LAST_ANALYSIS_SIGNALS, LAST_ANALYSIS_ONLY_NOTIFICATION_TIME, ANALYSIS_ONLY_INTERVAL, SIGNAL_THRESHOLD, GLOBAL_MACRO_CONTEXT, BASE_SCORE
    
    current_time = time.time()
    
    # 実行間隔のチェック
    if current_time - LAST_ANALYSIS_ONLY_NOTIFICATION_TIME < ANALYSIS_ONLY_INTERVAL:
        return

    # 分析結果が格納されているかチェック
    if not LAST_ANALYSIS_SIGNALS:
        logging.info("ℹ️ 分析結果がないため、最高スコア通知をスキップします。")
        LAST_ANALYSIS_ONLY_NOTIFICATION_TIME = current_time 
        return

    # スコアの降順でソートし、最高スコアのシグナルを取得
    sorted_signals = sorted(LAST_ANALYSIS_SIGNALS, key=lambda x: x.get('score', 0.0), reverse=True)
    best_signal = sorted_signals[0]
    
    best_score = best_signal.get('score', 0.0)
    current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
    
    # 最高スコアが取引閾値未満で、かつ最低限の分析スコア(例: BASE_SCORE+0.1)を超えている場合
    if best_score < current_threshold and best_score >= (BASE_SCORE + 0.1): 
        
        symbol = best_signal.get('symbol', 'N/A')
        timeframe = best_signal.get('timeframe', 'N/A')
        side = best_signal.get('side', 'long')

        # 通知メッセージの整形
        message = (
            f"📈 **分析結果 (最高スコア)** - 定時レポート\n"
            f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
            f"  - **確認日時**: {datetime.now(JST).strftime('%Y/%m/%d %H:%M:%S')} (JST)\n"
            f"  - **最高スコア銘柄**: <b>{symbol}</b> ({timeframe})\n"
            f"  - **最高スコア**: <code>{best_score * 100:.2f} / 100</code> ({side.capitalize()})\n"
            f"  - **取引閾値**: <code>{current_threshold * 100:.2f}</code> 点\n"
            f"  - **備考**: 取引閾値 (<code>{current_threshold * 100:.2f}</code>点) に満たなかったため取引はスキップされました。\n"
            f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
            f"<i>Bot Ver: {BOT_VERSION}</i>"
        )
        
        logging.info(f"ℹ️ 定時報告: 取引閾値未満の最高スコアを通知します: {symbol} ({side}) Score: {best_score:.2f}")
        await send_telegram_notification(message)
    else:
        # 最高スコアが閾値以上、または分析スコアが低すぎる場合は通知スキップ
        if best_score >= current_threshold:
            logging.info("ℹ️ 最高スコアが取引閾値以上のため、定時通知をスキップします (取引シグナルとして通知済みと想定)。")
        else:
             logging.info("ℹ️ 最高スコアが低すぎるため、定時通知をスキップします。")

    # 通知/ログ処理が完了したら時間を更新
    LAST_ANALYSIS_ONLY_NOTIFICATION_TIME = current_time

# ====================================================================================
# WEBSHARE FUNCTION (HTTP POST) 
# ====================================================================================

async def send_webshare_update(data: Dict[str, Any]):
    
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
    global EXCHANGE_CLIENT, IS_CLIENT_READY
    
    IS_CLIENT_READY = False
    
    if not API_KEY or not SECRET_KEY:
         logging.critical("❌ CCXT初期化スキップ: APIキー または SECRET_KEY が設定されていません。")
         return False
         
    # 既存のクライアントがあれば、リソースを解放する
    if EXCHANGE_CLIENT:
        try:
            await EXCHANGE_CLIENT.close()
            logging.info("✅ 既存のCCXTクライアントセッションを正常にクローズしました。")
        except Exception as e:
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

        timeout_ms = 30000 
        
        EXCHANGE_CLIENT = exchange_class({
            'apiKey': API_KEY,
            'secret': SECRET_KEY,
            'enableRateLimit': True,
            'options': options,
            'timeout': timeout_ms 
        })
        logging.info(f"✅ CCXTクライアントの初期化設定完了。リクエストタイムアウト: {timeout_ms/1000}秒。") 
        
        await EXCHANGE_CLIENT.load_markets() 
        
        # レバレッジの設定 (MEXC向け)
        if EXCHANGE_CLIENT.id == 'mexc':
            
            symbols_to_set_leverage = []
            
            # DEFAULT_SYMBOLSに含まれるCCXT標準シンボル (例: BTC/USDT) をベース/クォート通貨に分解
            default_base_quotes = {s.split('/')[0]: s.split('/')[1] for s in DEFAULT_SYMBOLS if '/' in s}
            
            for mkt in EXCHANGE_CLIENT.markets.values():
                # USDT建てのSwap/Future市場を探す
                if mkt['quote'] == 'USDT' and mkt['type'] in ['swap', 'future'] and mkt['active']:
                    # 市場の基本通貨が DEFAULT_SYMBOLS のベース通貨に含まれるかチェック
                    if mkt['base'] in default_base_quotes:
                        # set_leverageに渡すべきCCXTシンボル (例: BTC/USDT:USDT) をリストに追加
                        symbols_to_set_leverage.append(mkt['symbol'])
            
            # set_leverage() が openType と positionType の両方を要求するため、両方の設定を行います。
            for symbol in symbols_to_set_leverage:
                
                # openType: 2 は Cross Margin
                # positionType: 1 は Long (買い) ポジション用
                try:
                    await EXCHANGE_CLIENT.set_leverage(
                        LEVERAGE, 
                        symbol, 
                        params={'openType': 2, 'positionType': 1}
                    )
                    logging.info(f"✅ {symbol} のレバレッジを {LEVERAGE}x (Cross Margin / Long) に設定しました。")
                except Exception as e:
                    logging.warning(f"⚠️ {symbol} のレバレッジ/マージンモード設定 (Long) に失敗しました: {e}")
                
                # 💥 レートリミット対策として遅延を挿入
                await asyncio.sleep(LEVERAGE_SETTING_DELAY)

                # positionType: 2 は Short (売り) ポジション用
                try:
                    await EXCHANGE_CLIENT.set_leverage(
                        LEVERAGE, 
                        symbol, 
                        params={'openType': 2, 'positionType': 2}
                    )
                    logging.info(f"✅ {symbol} のレバレッジを {LEVERAGE}x (Cross Margin / Short) に設定しました。")
                except Exception as e:
                    logging.warning(f"⚠️ {symbol} のレバレッジ/マージンモード設定 (Short) に失敗しました: {e}")
                
                # 💥 レートリミット対策として遅延を挿入
                await asyncio.sleep(LEVERAGE_SETTING_DELAY)

            logging.info(f"✅ MEXCの主要な先物銘柄 ({len(symbols_to_set_leverage)}件) に対し、レバレッジを {LEVERAGE}x、マージンモードを 'cross' に設定しました。")
        
        logging.info(f"✅ CCXTクライアント ({CCXT_CLIENT_NAME}) を先物取引モードで初期化し、市場情報をロードしました。")
        IS_CLIENT_READY = True
        return True
        
    except ccxt.AuthenticationError as e:
        logging.critical(f"❌ CCXT初期化失敗 - 認証エラー: APIキー/シークレットを確認してください。{e}", exc_info=True)
    except ccxt.ExchangeNotAvailable as e:
        logging.critical(f"❌ CCXT初期化失敗 - 取引所接続エラー: サーバーが利用できません。{e}", exc_info=True)
    except ccxt.NetworkError as e:
        logging.critical(f"❌ CCXT初期化失敗 - ネットワークエラー/タイムアウト: 接続を確認してください。{e}", exc_info=True)
    except Exception as e:
        logging.critical(f"❌ CCXTクライアント初期化失敗 - 予期せぬエラー: {e}", exc_info=True)
        
    EXCHANGE_CLIENT = None
    return False


async def fetch_account_status() -> Dict:
    global EXCHANGE_CLIENT, ACCOUNT_EQUITY_USDT
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.error("❌ 口座ステータス取得失敗: CCXTクライアントが準備できていません。")
        return {'total_usdt_balance': 0.0, 'open_positions': [], 'error': True}
        
    try:
        balance = None
        if EXCHANGE_CLIENT.id == 'mexc':
            logging.info("ℹ️ MEXC: fetch_balance(type='swap') を使用して口座情報を取得します。")
            # MEXCはfetch_balance(type='swap')を使用
            balance = await EXCHANGE_CLIENT.fetch_balance(params={'defaultType': 'swap'})
        else:
            fetch_params = {'type': 'future'} if TRADE_TYPE == 'future' else {}
            balance = await EXCHANGE_CLIENT.fetch_balance(params=fetch_params)
            
        if not balance:
             raise Exception("Balance object is empty.")
             
        total_usdt_balance = balance.get('total', {}).get('USDT', 0.0)

        # MEXC特有のフォールバックロジック (infoからtotalEquityを探す)
        if EXCHANGE_CLIENT.id == 'mexc' and balance.get('info'):
            raw_data = balance['info']
            mexc_raw_data = None
            
            if isinstance(raw_data, dict) and 'data' in raw_data:
                 mexc_raw_data = raw_data.get('data')
            else:
                mexc_raw_data = raw_data
                
            mexc_data: Optional[Dict] = None
            
            if isinstance(mexc_raw_data, list) and len(mexc_raw_data) > 0:
                if isinstance(mexc_raw_data[0], dict):
                    mexc_data = mexc_raw_data[0]
            elif isinstance(mexc_raw_data, dict):
                 mexc_data = mexc_raw_data
                 
            if mexc_data:
                total_usdt_balance_fallback = 0.0
                if mexc_data.get('currency') == 'USDT':
                     total_usdt_balance_fallback = float(mexc_data.get('totalEquity', 0.0))
                elif mexc_data.get('assets') and isinstance(mexc_data['assets'], list):
                    for asset in mexc_data['assets']:
                        if asset.get('asset') == 'USDT':
                             total_usdt_balance_fallback = float(asset.get('totalEquity', 0.0))
                             break

                # どちらか大きい方を使用 (ccxtの計算が間違っている場合に備えて)
                if total_usdt_balance_fallback > total_usdt_balance:
                    total_usdt_balance = total_usdt_balance_fallback
                    logging.warning(f"⚠️ MEXCのフォールバックロジックでUSDT総資産を更新: {format_usdt(total_usdt_balance)}")
            

        # 総資産をグローバル変数に保存 (リスク管理に使用)
        ACCOUNT_EQUITY_USDT = float(total_usdt_balance)
        
        logging.info(f"💰 口座総資産 (USDT Equity): {format_usdt(ACCOUNT_EQUITY_USDT)}")
        
        # オープンポジション情報の取得
        # MEXCはfetch_positionsを使用。Binance/Bybitはfetch_positions_riskを使用
        positions: List[Dict] = []
        if EXCHANGE_CLIENT.has['fetchPositions']:
             positions = await EXCHANGE_CLIENT.fetch_positions()
        
        open_positions = []
        for p in positions:
            # ポジションサイズが0より大きい、かつUSDT建て先物シンボルのみを抽出
            if (p.get('contracts', 0) != 0 or p.get('info', {}).get('vol', 0) != 0) and \
               p.get('symbol', '').endswith('/USDT') and \
               p.get('type') in ['swap', 'future']:
                
                # 必要な情報を抽出/整形
                entry_price_raw = p.get('entryPrice', 0.0)
                contracts_raw = p.get('contracts', 0.0)
                
                # contractsが取れない場合、MEXC特有の 'vol' フィールドを試す
                if contracts_raw == 0 and EXCHANGE_CLIENT.id == 'mexc':
                    contracts_raw = float(p.get('info', {}).get('vol', 0.0))
                
                # 名目価値 (Notional Value) を計算
                notional_value = abs(contracts_raw * entry_price_raw)
                
                # side (long/short) の判定
                side = 'long' if contracts_raw > 0 else 'short'
                
                # 最終的なポジション情報 (ボットが管理すべき最低限の情報)
                open_positions.append({
                    'symbol': p['symbol'],
                    'side': side,
                    'contracts': contracts_raw,
                    'entry_price': entry_price_raw,
                    'filled_usdt': notional_value, # 名目価値
                    'liquidation_price': p.get('liquidationPrice', 0.0),
                    'timestamp': p.get('timestamp', int(time.time() * 1000)),
                    'stop_loss': 0.0, # ボット内で動的に設定/管理
                    'take_profit': 0.0, # ボット内で動的に設定/管理
                    'leverage': p.get('leverage', LEVERAGE), 
                    'raw_info': p, # デバッグ用に生の情報を保持
                })
        
        # グローバル変数 OPEN_POSITIONS を更新
        global OPEN_POSITIONS
        OPEN_POSITIONS = open_positions
        
        logging.info(f"✅ ポジション情報取得完了: 管理中のポジション数: {len(OPEN_POSITIONS)}件")
        
        return {
            'total_usdt_balance': ACCOUNT_EQUITY_USDT, 
            'open_positions': OPEN_POSITIONS, 
            'error': False
        }

    except ccxt.ExchangeNotAvailable as e:
        logging.error(f"❌ 口座ステータス取得失敗 - サーバー利用不可: {e}")
    except ccxt.NetworkError as e:
        logging.error(f"❌ 口座ステータス取得失敗 - ネットワークエラー: {e}")
    except Exception as e:
        logging.error(f"❌ 口座ステータス取得失敗 - 予期せぬエラー: {e}", exc_info=True)
        
    return {'total_usdt_balance': 0.0, 'open_positions': [], 'error': True}


async def fetch_top_volume_symbols() -> List[str]:
    """
    取引所の先物市場から、出来高に基づいて上位銘柄を取得し、監視リストを更新する。
    AttributesError対策として、tickerがNoneになるケースをハンドリングする。
    """
    global EXCHANGE_CLIENT, CURRENT_MONITOR_SYMBOLS
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY or SKIP_MARKET_UPDATE:
        logging.warning("⚠️ 市場更新をスキップします (クライアント未準備 or SKIP_MARKET_UPDATE=True)")
        return CURRENT_MONITOR_SYMBOLS
        
    try:
        logging.info("ℹ️ 出来高トップ銘柄リストの更新を開始します...")
        
        # 出来高 (Volume) を取得するため、すべてのティッカー情報を取得
        tickers_data: Dict[str, Any] = await EXCHANGE_CLIENT.fetch_tickers()
        
        # 🚨 fetch_tickersのAttributeError ('NoneType' object has no attribute 'keys') 対策
        if not tickers_data:
            logging.error("❌ fetch_tickersがNoneまたは空の辞書を返しました。リスト更新をスキップします。")
            return CURRENT_MONITOR_SYMBOLS
        
        # USDT建ての先物/スワップシンボルのみをフィルタリング
        future_tickers = {
            symbol: ticker 
            for symbol, ticker in tickers_data.items() 
            if symbol.endswith('/USDT') and 
               EXCHANGE_CLIENT.markets.get(symbol, {}).get('type') in ['swap', 'future'] and
               EXCHANGE_CLIENT.markets.get(symbol, {}).get('active')
        }
        
        # volume (USDT) に基づいてソート
        # 'quoteVolume' が利用可能な場合は使用、なければ 'volume' * 'last' (非USDT建ての場合は不正確)
        # ccxtのティッカーは通常 'quoteVolume' (USDT建て) を含む
        # ティッカーオブジェクトがNoneでないことを確認
        def get_quote_volume(ticker: Dict) -> float:
            if ticker is None: return 0.0
            
            # 優先度1: quoteVolume (USDT建ての出来高)
            volume = ticker.get('quoteVolume') 
            if volume is not None:
                try:
                    return float(volume)
                except (ValueError, TypeError):
                    pass # 型エラーは無視

            # 優先度2: baseVolume * last_price
            base_volume = ticker.get('baseVolume')
            last_price = ticker.get('last')
            if base_volume is not None and last_price is not None:
                try:
                    return float(base_volume) * float(last_price)
                except (ValueError, TypeError):
                    pass # 型エラーは無視

            return 0.0


        # quoteVolumeに基づいて降順ソート
        sorted_symbols = sorted(
            future_tickers.keys(), 
            key=lambda symbol: get_quote_volume(future_tickers[symbol]), 
            reverse=True
        )
        
        # TOP_SYMBOL_LIMIT に従って制限
        top_symbols = sorted_symbols[:TOP_SYMBOL_LIMIT]
        
        # DEFAULT_SYMBOLS のうち、まだリストに含まれていない主要な銘柄を追加
        for default_symbol in DEFAULT_SYMBOLS:
            if default_symbol not in top_symbols and default_symbol in EXCHANGE_CLIENT.markets:
                # ccxtの市場情報も確認し、アクティブな先物市場であることを確認
                market = EXCHANGE_CLIENT.markets[default_symbol]
                if market.get('type') in ['swap', 'future'] and market.get('active'):
                    top_symbols.append(default_symbol)


        # 最終的な監視リストを更新
        CURRENT_MONITOR_SYMBOLS = top_symbols
        logging.info(f"✅ 監視銘柄リストを更新しました。合計 {len(CURRENT_MONITOR_SYMBOLS)} 銘柄。")
        
    except ccxt.ExchangeNotAvailable as e:
        logging.error(f"❌ 出来高銘柄取得失敗 - サーバー利用不可: {e}")
    except ccxt.NetworkError as e:
        logging.error(f"❌ 出来高銘柄取得失敗 - ネットワークエラー: {e}")
    except Exception as e:
        # ccxt.fetch_tickersが返す形式が予期せぬもので、AttributeErrorなどが発生した場合をキャッチ
        logging.error(f"❌ 出来高銘柄取得失敗 - 予期せぬエラー: {e}", exc_info=True)
        
    return CURRENT_MONITOR_SYMBOLS

async def fetch_ohlcv(symbol: str, timeframe: str, limit: int) -> Optional[pd.DataFrame]:
    """指定された銘柄、時間足のOHLCVデータを取得する。"""
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        return None

    try:
        # ohlcv = [timestamp, open, high, low, close, volume]
        ohlcv_list = await EXCHANGE_CLIENT.fetch_ohlcv(
            symbol, 
            timeframe=timeframe, 
            limit=limit
        )
        
        if not ohlcv_list or len(ohlcv_list) < limit:
            logging.warning(f"⚠️ {symbol} - {timeframe}: 必要なOHLCVデータ ({limit}本) を取得できませんでした。({len(ohlcv_list)}本)")
            return None
            
        df = pd.DataFrame(ohlcv_list, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['datetime'] = pd.to_datetime(df['timestamp'], unit='ms')
        df.set_index('datetime', inplace=True)
        
        return df
        
    except ccxt.RateLimitExceeded as e:
        logging.warning(f"⚠️ {symbol} - {timeframe}: レートリミット超過。スキップします。")
        await asyncio.sleep(EXCHANGE_CLIENT.rateLimit / 1000.0) # 律儀にレートリミット分待つ
        return None
    except ccxt.ExchangeNotAvailable as e:
        logging.error(f"❌ {symbol} - {timeframe}: サーバー利用不可。")
    except ccxt.NetworkError as e:
        logging.error(f"❌ {symbol} - {timeframe}: ネットワークエラー。")
    except Exception as e:
        logging.error(f"❌ {symbol} - {timeframe}: OHLCV取得中に予期せぬエラー: {e}")
        
    return None

async def fetch_fgi_data() -> Dict:
    """Fear & Greed Index (FGI) を取得し、スコアリング用のプロキシ値に変換する。"""
    global FGI_API_URL, GLOBAL_MACRO_CONTEXT, FGI_PROXY_BONUS_MAX
    
    try:
        response = await asyncio.to_thread(requests.get, FGI_API_URL, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        if not data or 'data' not in data or not data['data']:
            raise Exception("FGIデータが空または予期せぬ形式です。")
            
        fgi_raw = data['data'][0]
        value = int(fgi_raw['value']) # 0 (Extreme Fear) から 100 (Extreme Greed)
        
        # FGIを[-1.0, 1.0]の範囲のプロキシ値に変換
        # 50が中立 (0.0)
        # 0が-1.0 (Extreme Fear)
        # 100が+1.0 (Extreme Greed)
        fgi_proxy = (value - 50) / 50.0 
        
        # スコアリングボーナスに変換
        # fgi_proxy * FGI_PROXY_BONUS_MAX (例: -0.05 ~ +0.05)
        fgi_score_influence = fgi_proxy * FGI_PROXY_BONUS_MAX 
        
        GLOBAL_MACRO_CONTEXT['fgi_proxy'] = fgi_score_influence
        GLOBAL_MACRO_CONTEXT['fgi_raw_value'] = f"{fgi_raw['value']} ({fgi_raw['value_classification']})"
        
        logging.info(f"✅ FGIデータ取得完了: {GLOBAL_MACRO_CONTEXT['fgi_raw_value']} -> スコア影響: {fgi_score_influence:+.4f}")
        return GLOBAL_MACRO_CONTEXT
        
    except requests.exceptions.RequestException as e:
        logging.error(f"❌ FGIデータ取得リクエストエラー: {e}")
    except Exception as e:
        logging.error(f"❌ FGIデータ処理エラー: {e}")
        
    # エラー時はデフォルト値を返す
    return GLOBAL_MACRO_CONTEXT

# ====================================================================================
# CORE TRADING LOGIC: SIGNAL GENERATION & SCORING
# ====================================================================================

def calculate_technical_indicators(df: pd.DataFrame, timeframe: str) -> Optional[pd.DataFrame]:
    """
    データフレームにテクニカル指標を追加する (Pandas TAを使用)。
    """
    
    # データをコピーして警告を抑制
    df = df.copy() 

    # 1. ATR (ボラティリティ)
    df.ta.atr(length=ATR_LENGTH, append=True)
    
    # 2. RSI (モメンタム)
    df.ta.rsi(length=14, append=True, col_names=('RSI_14',))
    
    # 3. MACD (トレンド/モメンタム)
    # MACD, MACDh (ヒストグラム), MACDs (シグナル) を生成
    df.ta.macd(fast=12, slow=26, signal=9, append=True, col_names=('MACD', 'MACDH', 'MACDS')) 
    
    # 4. SMA (長期トレンド)
    df.ta.sma(length=LONG_TERM_SMA_LENGTH, append=True, col_names=('SMA_LONG',))
    
    # 5. Bollinger Bands (ボラティリティ/価格の勢い)
    # BBANDS_L, BBANDS_M, BBANDS_U, BBANDS_W (幅)
    df.ta.bbands(length=20, std=2, append=True, col_names=('BBANDS_L', 'BBANDS_M', 'BBANDS_U', 'BBANDS_W', 'BBANDS_P'))
    
    # 6. OBV (出来高)
    df.ta.obv(append=True, col_names=('OBV',))
    
    # 必要な指標が生成されているか確認 (最後の行にNaNがないことを確認)
    required_cols = [f'ATR_{ATR_LENGTH}', 'RSI_14', 'MACDH', 'SMA_LONG', 'BBANDS_W']
    if df.iloc[-1][required_cols].isnull().any():
        logging.debug(f"⚠️ {timeframe}: テクニカル指標の計算結果にNaNが含まれるため、不十分なデータと判断します。")
        return None
        
    return df

def generate_signals_and_score(
    symbol: str, 
    timeframe: str, 
    df: pd.DataFrame, 
    macro_context: Dict
) -> List[Dict]:
    """
    指定されたデータフレームに基づき、ロング・ショートのシグナルとスコアを計算する。
    """
    if df is None or df.empty or len(df) < 200: 
        return []
    
    last_row = df.iloc[-1]
    
    # 価格情報
    current_price = last_row['close']
    
    # ボラティリティ指標
    atr_value = last_row[f'ATR_{ATR_LENGTH}']
    
    # テクニカル指標の値
    rsi_value = last_row['RSI_14']
    macd_h_value = last_row['MACDH']
    sma_long_value = last_row['SMA_LONG']
    bb_width_percent = last_row['BBANDS_W'] # BB幅 (Percentage)
    obv_value = last_row['OBV']
    
    # 前の足のMACDヒストグラム
    prev_macd_h_value = df.iloc[-2]['MACDH']
    prev_obv_value = df.iloc[-2]['OBV']
    
    
    signals = []
    
    for side in ['long', 'short']:
        
        final_score = BASE_SCORE # ベーススコアから開始
        tech_data: Dict[str, Any] = {'base_score': BASE_SCORE}
        
        # =========================================================================
        # 1. ボラティリティに基づく動的SL/TPの計算
        # =========================================================================
        
        # リスク幅 (ATRに基づく)
        risk_usdt_width = atr_value * ATR_MULTIPLIER_SL
        
        # 最低リスク幅を確保
        min_risk_usdt_width = current_price * MIN_RISK_PERCENT 
        risk_usdt_width = max(risk_usdt_width, min_risk_usdt_width)
        
        if side == 'long':
            stop_loss = current_price - risk_usdt_width
            take_profit = current_price + (risk_usdt_width * RR_RATIO_TARGET)
        else: # short
            stop_loss = current_price + risk_usdt_width
            take_profit = current_price - (risk_usdt_width * RR_RATIO_TARGET)

        # SL/TPが有効な値かチェック
        if stop_loss <= 0 or take_profit <= 0 or abs(stop_loss - take_profit) < 0.0001:
            logging.debug(f"⚠️ {symbol} ({timeframe}, {side}): SL/TPの計算が無効です。シグナルをスキップ。")
            continue
            
        # 実際のRR比率を計算 (目標RR比率と乖離する場合がある)
        actual_risk_width = abs(current_price - stop_loss)
        actual_reward_width = abs(take_profit - current_price)
        rr_ratio = actual_reward_width / actual_risk_width if actual_risk_width > 0 else 0.0
        
        tech_data['atr_value'] = atr_value
        tech_data['risk_usdt_width'] = risk_usdt_width
        tech_data['rr_ratio_actual'] = rr_ratio
        
        # 清算価格の計算
        liquidation_price = calculate_liquidation_price(
            current_price, 
            LEVERAGE, 
            side, 
            MIN_MAINTENANCE_MARGIN_RATE
        )
        
        # SLが清算価格より手前にあるかチェック (安全性の確認)
        is_safe_sl = False
        if side == 'long':
             is_safe_sl = stop_loss > liquidation_price * 1.01 # 1%バッファ
        else: # short
             is_safe_sl = stop_loss < liquidation_price * 0.99 # 1%バッファ
             
        if not is_safe_sl:
            logging.debug(f"⚠️ {symbol} ({timeframe}, {side}): SL ({format_price(stop_loss)}) が清算価格 ({format_price(liquidation_price)}) に近すぎます。シグナルをスキップ。")
            continue
            
        # =========================================================================
        # 2. スコアリングロジックの適用 (実践的ロジック)
        # =========================================================================

        # a. 長期トレンドとの方向一致 (LONG_TERM_REVERSAL_PENALTY: +/-0.20)
        trend_score = 0.0
        if side == 'long':
            # ロングの場合: 価格が長期SMAより上 (+Bonus) or 下 (-Penalty)
            if current_price > sma_long_value:
                trend_score = LONG_TERM_REVERSAL_PENALTY 
            elif current_price < sma_long_value:
                trend_score = -LONG_TERM_REVERSAL_PENALTY 
        else: # short
            # ショートの場合: 価格が長期SMAより下 (+Bonus) or 上 (-Penalty)
            if current_price < sma_long_value:
                trend_score = LONG_TERM_REVERSAL_PENALTY 
            elif current_price > sma_long_value:
                trend_score = -LONG_TERM_REVERSAL_PENALTY
        
        final_score += trend_score
        tech_data['long_term_reversal_penalty_value'] = trend_score
        
        # b. MACDヒストグラムの方向一致とモメンタム加速 (MACD_CROSS_PENALTY: +/-0.15)
        macd_score = 0.0
        if side == 'long':
            # ロングの場合: MACDHが陽性 (+Bonus) or 陰性 (-Penalty)
            if macd_h_value > 0 and prev_macd_h_value <= 0: # ゼロラインクロス (新規エントリーの確証)
                macd_score = MACD_CROSS_PENALTY
            elif macd_h_value > 0:
                 macd_score = MACD_CROSS_PENALTY / 2.0
            elif macd_h_value <= 0:
                 macd_score = -MACD_CROSS_PENALTY 
        else: # short
            # ショートの場合: MACDHが陰性 (+Bonus) or 陽性 (-Penalty)
            if macd_h_value < 0 and prev_macd_h_value >= 0: # ゼロラインクロス (新規エントリーの確証)
                macd_score = MACD_CROSS_PENALTY
            elif macd_h_value < 0:
                 macd_score = MACD_CROSS_PENALTY / 2.0
            elif macd_h_value >= 0:
                 macd_score = -MACD_CROSS_PENALTY
                 
        final_score += macd_score
        tech_data['macd_penalty_value'] = macd_score
        
        # c. RSIモメンタム加速 (RSI_MOMENTUM_LOW: 40/60)
        rsi_score = 0.0
        if side == 'long':
            # ロングの場合: RSI > 50、かつRSI > RSI_MOMENTUM_LOW (40)
            if rsi_value > 50 and rsi_value > RSI_MOMENTUM_LOW:
                rsi_score = STRUCTURAL_PIVOT_BONUS * 1.5 # 0.075
        else: # short
            # ショートの場合: RSI < 50、かつRSI < (100 - RSI_MOMENTUM_LOW) (60)
            if rsi_value < 50 and rsi_value < (100 - RSI_MOMENTUM_LOW):
                rsi_score = STRUCTURAL_PIVOT_BONUS * 1.5 # 0.075
                
        final_score += rsi_score
        tech_data['rsi_momentum_bonus_value'] = rsi_score
        
        # d. OBVによる出来高確証 (OBV_MOMENTUM_BONUS: +0.04)
        obv_score = 0.0
        if side == 'long':
             # OBVが上昇傾向
            if obv_value > prev_obv_value:
                 obv_score = OBV_MOMENTUM_BONUS
        else: # short
             # OBVが下降傾向
            if obv_value < prev_obv_value:
                 obv_score = OBV_MOMENTUM_BONUS
                 
        final_score += obv_score
        tech_data['obv_momentum_bonus_value'] = obv_score
        
        # e. ボラティリティペナルティ
        volatility_penalty = 0.0
        # BB幅 (BBANDS_W) が価格の一定割合を超えているかチェック
        # BBANDS_Wはパーセンテージで提供されている
        if bb_width_percent > (VOLATILITY_BB_PENALTY_THRESHOLD * 100):
            # BB幅が大きすぎる (ボラティリティ過熱)
            volatility_penalty = -STRUCTURAL_PIVOT_BONUS 
        
        final_score += volatility_penalty
        tech_data['volatility_penalty_value'] = volatility_penalty
        
        # f. 構造的優位性ボーナス (ベース)
        final_score += STRUCTURAL_PIVOT_BONUS
        tech_data['structural_pivot_bonus'] = STRUCTURAL_PIVOT_BONUS
        
        # g. 流動性ボーナス
        liquidity_bonus = 0.0
        # 監視銘柄リストにおける順位に応じてボーナスを与える
        try:
            rank = CURRENT_MONITOR_SYMBOLS.index(symbol) if symbol in CURRENT_MONITOR_SYMBOLS else TOP_SYMBOL_LIMIT
            # 順位が高いほどボーナスも高い (例: 1位でLIQUIDITY_BONUS_MAX, 40位で0)
            liquidity_bonus = LIQUIDITY_BONUS_MAX * (1 - (rank / TOP_SYMBOL_LIMIT))
            if liquidity_bonus < 0: liquidity_bonus = 0.0
        except:
            liquidity_bonus = 0.0
            
        final_score += liquidity_bonus
        tech_data['liquidity_bonus_value'] = liquidity_bonus
        
        # h. マクロ環境の影響 (FGIプロキシボーナス)
        # GLOBAL_MACRO_CONTEXT の fgi_proxy は既にスコアに影響する値に変換されている
        fgi_score_influence = macro_context.get('fgi_proxy', 0.0) 
        
        # ロングの場合: fgi_score_influenceをそのまま加算
        # ショートの場合: fgi_score_influenceを反転して加算 (市場の楽観はショートにとってマイナス)
        macro_influence = fgi_score_influence if side == 'long' else -fgi_score_influence
        
        # Forexの影響 (現時点では0だが、将来的な拡張を見越して保持)
        forex_influence = macro_context.get('forex_bonus', 0.0)
        macro_influence += forex_influence
        
        final_score += macro_influence
        tech_data['sentiment_fgi_proxy_bonus'] = macro_influence
        
        # =========================================================================
        # 3. 最終スコアの確定とシグナル生成
        # =========================================================================
        
        # スコアを [0.0, 1.0] に正規化 (念のため)
        final_score = max(0.0, min(1.0, final_score))
        
        signals.append({
            'symbol': symbol,
            'timeframe': timeframe,
            'side': side,
            'score': final_score,
            'entry_price': current_price,
            'stop_loss': stop_loss,
            'take_profit': take_profit,
            'liquidation_price': liquidation_price, # 清算価格
            'rr_ratio': rr_ratio,
            'tech_data': tech_data,
        })
        
    return signals


async def run_analysis_and_generate_signals(macro_context: Dict) -> List[Dict]:
    """
    監視対象の全銘柄、全時間足で分析を行い、取引可能なシグナルを生成する。
    """
    
    global CURRENT_MONITOR_SYMBOLS, LAST_ANALYSIS_SIGNALS
    
    if not IS_CLIENT_READY:
         logging.critical("❌ 分析実行失敗: CCXTクライアントが準備できていません。")
         return []
         
    all_signals: List[Dict] = []
    
    # 銘柄と時間足の組み合わせをイテレート
    # 実行速度を考慮し、時間足ごとに並列処理を行う。
    
    for symbol in CURRENT_MONITOR_SYMBOLS:
        
        tasks = []
        for tf in TARGET_TIMEFRAMES:
            limit = REQUIRED_OHLCV_LIMITS.get(tf, 1000)
            tasks.append(fetch_ohlcv(symbol, tf, limit))
            
        ohlcv_results = await asyncio.gather(*tasks)
        
        # すべての時間足のOHLCVデータが揃ったら分析を実行
        for i, tf in enumerate(TARGET_TIMEFRAMES):
            df = ohlcv_results[i]
            if df is None:
                continue
                
            # テクニカル指標の計算
            df_ta = calculate_technical_indicators(df, tf)
            if df_ta is None:
                continue
                
            # シグナルとスコアの生成
            signals = generate_signals_and_score(symbol, tf, df_ta, macro_context)
            all_signals.extend(signals)
            
            
    logging.info(f"✅ 全銘柄/全時間足の分析が完了しました。合計 {len(all_signals)} 件のシグナルを生成。")
    
    # 🚨 全てのシグナルをグローバルに保存 (定時報告用)
    LAST_ANALYSIS_SIGNALS = all_signals 

    # スコアの降順でソートし、取引閾値以上のシグナルのみを抽出
    current_threshold = get_current_threshold(macro_context)
    
    tradable_signals = [
        s for s in all_signals 
        if s['score'] >= current_threshold
    ]
    
    # スコアで最終ソート
    tradable_signals = sorted(
        tradable_signals, 
        key=lambda x: x['score'], 
        reverse=True
    )

    logging.info(f"✅ 取引閾値 ({current_threshold*100:.0f}点) を超えたシグナル数: {len(tradable_signals)} 件。")
    
    return tradable_signals[:TOP_SIGNAL_COUNT] # 最もスコアが高いTOP_SIGNAL_COUNT件のみを返す

# ====================================================================================
# CORE TRADING LOGIC: ENTRY & EXIT
# ====================================================================================

async def execute_entry_order(signal: Dict) -> Dict:
    """
    計算されたシグナルに基づき、取引所に対して成行注文 (Market Order) を実行する。
    """
    global EXCHANGE_CLIENT
    
    if TEST_MODE or not IS_CLIENT_READY:
        logging.warning(f"⚠️ TEST_MODE/CLIENT_NOT_READYのため、{signal['symbol']} ({signal['side']}) の注文をスキップします。")
        return {'status': 'skip', 'error_message': 'TEST_MODE is active or Client not ready.', 'filled_usdt': FIXED_NOTIONAL_USDT}


    symbol = signal['symbol']
    side = signal['side']
    entry_price = signal['entry_price']
    
    # 1. 注文数量 (Contracts) の計算
    # 🚨 CCXTでは、先物取引でポジションを持つ際、USDT名目価値 (Notional Value) ではなく、
    # 契約数 (Contracts) または基準通貨量 (Amount) で指定する必要がある。
    # 固定ロット (FIXED_NOTIONAL_USDT) を使用し、現在の価格で割って数量を計算する。
    
    # 必要数量 (契約数)
    amount_raw = FIXED_NOTIONAL_USDT / entry_price 
    
    # 2. 取引所のロットサイズ (Precision) に数量を調整
    market = EXCHANGE_CLIENT.markets.get(symbol)
    if not market:
        error_msg = f"❌ {symbol} の市場情報が見つかりません。"
        logging.error(error_msg)
        return {'status': 'error', 'error_message': error_msg, 'filled_usdt': 0.0}

    # amountPrecision: 数量の小数点以下の精度
    amount_precision = market.get('precision', {}).get('amount')
    
    if amount_precision is not None:
        # 小数点以下の桁数に丸める (例: amount_precision=4 -> 小数点以下4桁)
        # ccxt.decimal_to_precision を使用するのが正確だが、ここでは簡易的に
        try:
             amount = float(EXCHANGE_CLIENT.amount_to_precision(symbol, amount_raw))
        except Exception as e:
            logging.error(f"❌ 数量の丸め処理 (amount_to_precision) 失敗: {e}")
            amount = amount_raw # 丸めずに続行
    else:
        amount = amount_raw
        
    # 3. 注文方向の決定
    order_side = 'buy' if side == 'long' else 'sell'
    
    # 4. 最小注文サイズ (Min Amount) のチェック
    min_amount = market.get('limits', {}).get('amount', {}).get('min')
    if min_amount is not None and amount < min_amount:
        error_msg = f"❌ 注文数量 {amount:.4f} が最小注文サイズ {min_amount:.4f} を下回っています。注文をスキップします。"
        logging.error(error_msg)
        return {'status': 'error', 'error_message': error_msg, 'filled_usdt': 0.0}

    
    # 5. 注文パラメータの設定
    params = {}
    if EXCHANGE_CLIENT.id == 'mexc':
        # MEXCでは先物取引でポジションを持つ際、ポジションの方向を指定する必要がある
        params['positionType'] = 1 if side == 'long' else 2 # 1: Long, 2: Short
        # MEXCはトリガー機能がないため、SL/TPは個別に監視/手動で設定する

    logging.info(f"➡️ {symbol} ({side}): {order_side} 成行注文 (Amount: {amount:.4f}, Notional: {format_usdt(FIXED_NOTIONAL_USDT)} USDT) を送信します...")

    try:
        # 6. 成行注文の実行
        order = await EXCHANGE_CLIENT.create_order(
            symbol=symbol,
            type='market',
            side=order_side,
            amount=amount,
            params=params
        )
        
        # 7. 約定情報の確認
        filled_amount = order.get('filled', 0.0)
        filled_price = order.get('price', entry_price) # 成行注文なので、'price'は平均約定価格または None
        if filled_amount == 0.0:
            # 注文が完全に約定しなかった場合
            error_msg = f"⚠️ {symbol} 注文は送信されましたが、約定数量が0です (Status: {order.get('status')})。"
            logging.error(error_msg)
            return {'status': 'error', 'error_message': error_msg, 'filled_usdt': 0.0}
            
        # 8. グローバルなオープンポジションリストに、新しく持ったポジションを追加
        
        # 実際の約定価格と名目価値を再計算
        actual_entry_price = filled_price if filled_price > 0 else entry_price 
        filled_usdt_notional = filled_amount * actual_entry_price
        
        new_position = {
            'symbol': symbol,
            'side': side,
            'contracts': filled_amount if side == 'long' else -filled_amount, # ロングは正、ショートは負
            'entry_price': actual_entry_price,
            'filled_usdt': filled_usdt_notional, 
            'liquidation_price': calculate_liquidation_price(actual_entry_price, LEVERAGE, side, MIN_MAINTENANCE_MARGIN_RATE),
            'timestamp': int(time.time() * 1000),
            # SL/TPはシグナルから引き継ぎ
            'stop_loss': signal['stop_loss'],
            'take_profit': signal['take_profit'],
            'leverage': LEVERAGE, 
            'id': order.get('id', str(uuid.uuid4())),
            'rr_ratio': signal['rr_ratio'],
            'raw_order': order,
        }
        
        OPEN_POSITIONS.append(new_position)
        
        logging.info(f"✅ {symbol} ({side}) の成行注文が成功しました。約定数量: {filled_amount:.4f} @ {format_price(actual_entry_price)}")
        
        return {
            'status': 'ok', 
            'order': order, 
            'filled_amount': filled_amount,
            'filled_usdt': filled_usdt_notional,
            'actual_entry_price': actual_entry_price
        }

    except ccxt.ExchangeNotAvailable as e:
        error_msg = f"❌ 注文失敗 - サーバー利用不可: {e}"
        logging.error(error_msg)
        return {'status': 'error', 'error_message': error_msg, 'filled_usdt': 0.0}
    except ccxt.InsufficientFunds as e:
        error_msg = f"❌ 注文失敗 - 資金不足: ロットサイズを確認してください。{e}"
        logging.error(error_msg)
        return {'status': 'error', 'error_message': error_msg, 'filled_usdt': 0.0}
    except ccxt.InvalidOrder as e:
        # 🚨 致命的エラー修正強化: 注文失敗エラー (Amount can not be less than zero など) 対策
        error_msg = f"❌ 注文失敗 - 不正な注文: 数量または価格が不正です。{e}"
        logging.error(error_msg)
        return {'status': 'error', 'error_message': error_msg, 'filled_usdt': 0.0}
    except ccxt.NetworkError as e:
        error_msg = f"❌ 注文失敗 - ネットワークエラー: {e}"
        logging.error(error_msg)
        return {'status': 'error', 'error_message': error_msg, 'filled_usdt': 0.0}
    except Exception as e:
        error_msg = f"❌ 注文失敗 - 予期せぬエラー: {e}"
        logging.critical(error_msg, exc_info=True)
        return {'status': 'error', 'error_message': error_msg, 'filled_usdt': 0.0}


async def close_position(position: Dict, exit_price: float, exit_type: str) -> Dict:
    """
    既存のポジションを指定価格と決済タイプで決済する。
    """
    global EXCHANGE_CLIENT
    
    if TEST_MODE or not IS_CLIENT_READY:
        logging.warning(f"⚠️ TEST_MODE/CLIENT_NOT_READYのため、{position['symbol']} のポジション決済をスキップします。")
        return {
            'status': 'skip', 
            'error_message': 'TEST_MODE is active or Client not ready.',
            'entry_price': position['entry_price'],
            'exit_price': exit_price,
            'pnl_usdt': 0.0,
            'pnl_rate': 0.0,
            'exit_type': exit_type,
            'filled_amount': position['contracts'] # スキップでも契約数は保持
        }

    symbol = position['symbol']
    contracts_raw = position['contracts']
    
    # 決済に必要な数量 (絶対値)
    amount_to_close = abs(contracts_raw)
    
    # 決済方向 (ポジションと逆)
    order_side = 'sell' if contracts_raw > 0 else 'buy'
    
    # 注文タイプ: 成行 (Market)
    order_type = 'market'
    
    logging.info(f"➡️ {symbol} のポジション決済 ({exit_type}) を実行します。Side: {order_side}, Amount: {amount_to_close:.4f}")

    params = {}
    if EXCHANGE_CLIENT.id == 'mexc':
        # MEXCの場合、positionTypeを再設定する必要がある（成行決済の場合は不要な可能性もあるが、念のため）
        # クローズはポジション全体に対する操作なので、paramsは省略する
        pass 
    
    try:
        # 1. 注文の実行 (成行注文でポジションを閉じる)
        close_order = await EXCHANGE_CLIENT.create_order(
            symbol=symbol,
            type=order_type,
            side=order_side,
            amount=amount_to_close,
            params=params
        )

        # 2. 約定情報の確認と損益計算
        filled_amount = close_order.get('filled', 0.0)
        actual_exit_price = close_order.get('price', exit_price) # 平均約定価格
        
        if filled_amount == 0.0:
            error_msg = f"⚠️ {symbol} 決済注文は送信されましたが、約定数量が0です (Status: {close_order.get('status')})。"
            logging.error(error_msg)
            # ポジションリストからは削除しないが、エラーとして返す
            return {'status': 'error', 'error_message': error_msg, 'exit_type': exit_type, 'filled_amount': amount_to_close}

        # 損益計算 (P&L)
        entry_price = position['entry_price']
        filled_contracts = filled_amount 
        
        # PnL計算 (Long: Exit-Entry, Short: Entry-Exit) * contracts
        if position['side'] == 'long':
            price_diff = actual_exit_price - entry_price
        else: # short
            price_diff = entry_price - actual_exit_price
            
        pnl_usdt = price_diff * filled_contracts
        
        # PnL率 (レバレッジ考慮後の証拠金に対するリターン率)
        # 簡易的に名目価値 (filled_usdt) を元本とみなし、レバレッジで割って証拠金とする
        initial_margin = position['filled_usdt'] / LEVERAGE 
        pnl_rate = pnl_usdt / initial_margin if initial_margin > 0 else 0.0
        
        # 3. グローバルなポジションリストから削除
        global OPEN_POSITIONS
        OPEN_POSITIONS = [p for p in OPEN_POSITIONS if p.get('id') != position.get('id') and p['symbol'] != symbol]
        
        logging.info(f"✅ {symbol} ポジション決済完了 ({exit_type})。PNL: {format_usdt(pnl_usdt)} USDT ({pnl_rate*100:.2f}%)。")

        return {
            'status': 'ok', 
            'entry_price': entry_price,
            'exit_price': actual_exit_price,
            'pnl_usdt': pnl_usdt,
            'pnl_rate': pnl_rate,
            'exit_type': exit_type,
            'filled_amount': filled_contracts,
            'raw_order': close_order
        }

    except ccxt.ExchangeNotAvailable as e:
        error_msg = f"❌ 決済失敗 - サーバー利用不可: {e}"
        logging.error(error_msg)
    except ccxt.NetworkError as e:
        error_msg = f"❌ 決済失敗 - ネットワークエラー: {e}"
        logging.error(error_msg)
    except Exception as e:
        error_msg = f"❌ 決済失敗 - 予期せぬエラー: {e}"
        logging.critical(error_msg, exc_info=True)

    # 失敗時はエラー情報のみを返す (ポジションリストからは削除しない)
    return {'status': 'error', 'error_message': error_msg, 'exit_type': exit_type, 'filled_amount': amount_to_close}


async def position_monitor_and_update_sltp():
    """
    既存のオープンポジションを監視し、TP/SLに到達しているか、または清算リスクがないかをチェックする。
    """
    global EXCHANGE_CLIENT, OPEN_POSITIONS
    
    if not OPEN_POSITIONS or not IS_CLIENT_READY:
        return
        
    logging.info(f"👀 ポジション監視を開始します。監視中のポジション数: {len(OPEN_POSITIONS)}")

    try:
        # 1. リアルタイム価格の取得
        symbols_to_monitor = [p['symbol'] for p in OPEN_POSITIONS]
        tickers_data: Dict[str, Any] = await EXCHANGE_CLIENT.fetch_tickers(symbols_to_monitor)
        
        # 🚨 fetch_tickersのAttributeError ('NoneType' object has no attribute 'keys') 対策
        if not tickers_data:
             logging.error("❌ fetch_tickersがNoneまたは空の辞書を返しました。価格取得に失敗したため、監視をスキップします。")
             return
             
        # 2. 各ポジションのチェック
        
        # 決済処理が必要なポジションを一時的に保持するリスト
        positions_to_close = [] 
        
        for pos in OPEN_POSITIONS:
            symbol = pos['symbol']
            side = pos['side']
            stop_loss = pos['stop_loss']
            take_profit = pos['take_profit']
            liquidation_price = pos['liquidation_price']
            
            ticker = tickers_data.get(symbol)
            
            if not ticker:
                logging.warning(f"⚠️ {symbol} の現在価格が取得できませんでした。スキップします。")
                continue
                
            current_price = ticker.get('last')
            
            if current_price is None or current_price <= 0:
                logging.warning(f"⚠️ {symbol} の現在価格 (last) が無効です。スキップします。")
                continue
            
            # --- TP/SL判定 ---
            
            exit_needed = False
            exit_type = None
            
            if side == 'long':
                # ロングの場合: 価格がTP以上 OR 価格がSL以下
                if current_price >= take_profit:
                    exit_needed = True
                    exit_type = "TP (テイクプロフィット) 到達"
                elif current_price <= stop_loss:
                    exit_needed = True
                    exit_type = "SL (ストップロス) 到達"
                    
            else: # short
                # ショートの場合: 価格がSL以上 OR 価格がTP以下
                if current_price >= stop_loss:
                    exit_needed = True
                    exit_type = "SL (ストップロス) 到達"
                elif current_price <= take_profit:
                    exit_needed = True
                    exit_type = "TP (テイクプロフィット) 到達"
                    
            if exit_needed:
                positions_to_close.append({
                    'position': pos, 
                    'exit_price': current_price, 
                    'exit_type': exit_type
                })
                
            # --- 清算リスク判定 ---
            # SLが清算価格より手前にあるため、基本的には清算は発生しないはずだが、念のため。
            if side == 'long':
                # 価格が清算価格に近づいているか
                if current_price <= liquidation_price * 1.05: # 清算価格の5%以内
                    logging.warning(f"🚨 {symbol} (Long): 清算価格 ({format_price(liquidation_price)}) に接近中！現在価格: {format_price(current_price)}")
            else: # short
                if current_price >= liquidation_price * 0.95: # 清算価格の5%以内
                    logging.warning(f"🚨 {symbol} (Short): 清算価格 ({format_price(liquidation_price)}) に接近中！現在価格: {format_price(current_price)}")
                    
        
        # 3. 決済が必要なポジションを順次決済
        for item in positions_to_close:
            pos = item['position']
            exit_price = item['exit_price']
            exit_type = item['exit_type']
            
            # 決済実行
            close_result = await close_position(pos, exit_price, exit_type)
            
            # 決済結果をTelegramで通知
            if close_result['status'] != 'error':
                 # 決済シグナルとしてログに記録
                log_signal(pos, "ポジション決済", close_result)
                
                # 通知メッセージの作成 (pos辞書にはSL/TP/Entryなどの情報が含まれている)
                message = format_telegram_message(
                    signal=pos, 
                    context="ポジション決済", 
                    current_threshold=get_current_threshold(GLOBAL_MACRO_CONTEXT),
                    trade_result=close_result,
                    exit_type=exit_type
                )
                await send_telegram_notification(message)
            else:
                logging.error(f"❌ {pos['symbol']} のポジション決済 ({exit_type}) に失敗しました: {close_result['error_message']}")


    except ccxt.ExchangeNotAvailable as e:
        logging.error(f"❌ ポジション監視失敗 - サーバー利用不可: {e}")
    except ccxt.NetworkError as e:
        logging.error(f"❌ ポジション監視失敗 - ネットワークエラー: {e}")
    except Exception as e:
        logging.critical(f"❌ ポジション監視中に予期せぬ致命的エラー: {e}", exc_info=True)


# ====================================================================================
# MAIN BOT LOOP & SCHEDULERS
# ====================================================================================

async def main_bot_loop():
    """
    BOTのメイン処理ループ: 
    1. 市場データ・マクロ環境の取得/更新
    2. テクニカル分析とシグナル生成
    3. 取引実行
    """
    global LAST_SUCCESS_TIME, IS_FIRST_MAIN_LOOP_COMPLETED, LAST_SIGNAL_TIME, OPEN_POSITIONS, GLOBAL_MACRO_CONTEXT
    
    # 1. CCXTクライアントの初期化
    if not IS_CLIENT_READY:
        logging.info("🛠️ CCXTクライアントの初期化を行います...")
        if not await initialize_exchange_client():
            logging.critical("❌ CCXTクライアントの初期化に失敗しました。メインループをスキップします。")
            return

    # 2. マクロ環境データの取得 (FGIなど)
    GLOBAL_MACRO_CONTEXT = await fetch_fgi_data()

    # 3. 口座ステータスの取得
    account_status = await fetch_account_status()
    
    if account_status.get('error'):
         logging.critical("❌ 口座ステータス取得失敗。取引を停止します。")
         # 初回起動時のメッセージを通知
         if not IS_FIRST_MAIN_LOOP_COMPLETED:
            await send_telegram_notification(
                format_startup_message(
                    account_status, 
                    GLOBAL_MACRO_CONTEXT, 
                    len(CURRENT_MONITOR_SYMBOLS), 
                    get_current_threshold(GLOBAL_MACRO_CONTEXT)
                )
            )
            IS_FIRST_MAIN_LOOP_COMPLETED = True
         return
         
    # 4. 監視対象銘柄リストの更新 (出来高ベース)
    if not SKIP_MARKET_UPDATE:
        await fetch_top_volume_symbols()

    # 初回起動完了通知 (ここで通知することで、必要な情報が揃ったことを保証)
    if not IS_FIRST_MAIN_LOOP_COMPLETED:
        await send_telegram_notification(
            format_startup_message(
                account_status, 
                GLOBAL_MACRO_CONTEXT, 
                len(CURRENT_MONITOR_SYMBOLS), 
                get_current_threshold(GLOBAL_MACRO_CONTEXT)
            )
        )
        IS_FIRST_MAIN_LOOP_COMPLETED = True


    # 5. 取引シグナルの分析と生成
    tradable_signals = await run_analysis_and_generate_signals(GLOBAL_MACRO_CONTEXT)
    
    if not tradable_signals:
        logging.info("ℹ️ 取引可能なシグナルはありませんでした。")
        await notify_highest_analysis_score() # 定時報告チェック
        LAST_SUCCESS_TIME = time.time()
        return

    # 6. ポジションチェックと取引実行
    
    # 既にポジションを持っている銘柄リスト
    open_symbol_list = [p['symbol'] for p in OPEN_POSITIONS]
    
    current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)

    for signal in tradable_signals:
        symbol = signal['symbol']
        side = signal['side']
        
        # クールダウンチェック: 過去の取引から一定時間経過しているか
        cooldown_passed = (time.time() - LAST_SIGNAL_TIME.get(symbol, 0.0)) > TRADE_SIGNAL_COOLDOWN
        
        # 既にポジションを持っている銘柄はスキップ
        if symbol in open_symbol_list:
            logging.warning(f"⚠️ {symbol}: 既にポジションを保有しているため、新規シグナル ({side}) をスキップします。")
            continue
            
        # クールダウン中の銘柄はスキップ (連続取引防止)
        if not cooldown_passed:
            logging.info(f"ℹ️ {symbol}: クールダウン期間中のため、新規シグナル ({side}) をスキップします。")
            continue

        logging.info(f"🔥 **取引シグナル発見**: {symbol} ({side}) Score: {signal['score'] * 100:.2f} / {current_threshold * 100:.2f} (RR: 1:{signal['rr_ratio']:.2f})")
        
        # 注文実行
        trade_result = await execute_entry_order(signal)
        
        # 注文結果をTelegramで通知
        if trade_result['status'] != 'skip':
            # 注文実行の成功/失敗に関わらず、シグナルをログに記録
            log_signal(signal, "取引シグナル", trade_result)
            
            # 通知メッセージの作成
            message = format_telegram_message(
                signal, 
                "取引シグナル", 
                current_threshold, 
                trade_result=trade_result
            )
            await send_telegram_notification(message)
            
            # 成功した場合のみクールダウン時間を更新
            if trade_result['status'] == 'ok':
                LAST_SIGNAL_TIME[symbol] = time.time()
                # 成功した場合は、このメインループでは他の取引は実行しない
                break 

    # 7. WebShareの定時更新
    await check_and_send_webshare_update()
    
    LAST_SUCCESS_TIME = time.time()
    logging.info(f"✅ メインBOTループが正常に完了しました。次回実行まで {LOOP_INTERVAL} 秒待機。")

async def check_and_send_webshare_update():
    """
    WebShareの更新間隔をチェックし、必要であればデータを送信する。
    """
    global LAST_WEBSHARE_UPLOAD_TIME, WEBSHARE_UPLOAD_INTERVAL
    
    current_time = time.time()
    
    if current_time - LAST_WEBSHARE_UPLOAD_TIME < WEBSHARE_UPLOAD_INTERVAL:
        return
        
    webshare_data = {
        'version': BOT_VERSION,
        'timestamp_jst': datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
        'exchange': CCXT_CLIENT_NAME.upper(),
        'leverage': LEVERAGE,
        'test_mode': TEST_MODE,
        'equity_usdt': ACCOUNT_EQUITY_USDT,
        'monitoring_symbols_count': len(CURRENT_MONITOR_SYMBOLS),
        'fgi_raw_value': GLOBAL_MACRO_CONTEXT.get('fgi_raw_value', 'N/A'),
        'fgi_score_influence': GLOBAL_MACRO_CONTEXT.get('fgi_proxy', 0.0),
        'current_threshold': get_current_threshold(GLOBAL_MACRO_CONTEXT),
        'open_positions': [
            {
                'symbol': p['symbol'],
                'side': p['side'],
                'entry_price': p['entry_price'],
                'filled_usdt': p['filled_usdt'],
                'sl': p['stop_loss'],
                'tp': p['take_profit'],
                'rr': p.get('rr_ratio', 0.0),
            } 
            for p in OPEN_POSITIONS
        ],
        'last_signals_summary': [
            {
                'symbol': s['symbol'],
                'side': s['side'],
                'score': s['score'],
                'timeframe': s['timeframe'],
                'entry_price': s.get('entry_price', 0.0)
            }
            for s in sorted(LAST_ANALYSIS_SIGNALS, key=lambda x: x['score'], reverse=True)[:5]
        ]
    }
    
    await send_webshare_update(webshare_data)
    
    LAST_WEBSHARE_UPLOAD_TIME = current_time


async def main_bot_scheduler():
    """メインループを定期的に実行するためのスケジューラ"""
    while True:
        try:
            await main_bot_loop()
        except Exception as e:
            logging.critical(f"❌ メインBOTスケジューラ内で致命的なエラーが発生しました: {e}", exc_info=True)
            # エラー発生時も待機し、次のループで再試行
        await asyncio.sleep(LOOP_INTERVAL)

async def position_monitor_scheduler():
    """ポジション監視専用のスケジューラ"""
    # メインループ内の position_monitor_and_update_sltp() の実行間隔を短くするために用意
    while True:
        if OPEN_POSITIONS:
            # 短い間隔でポジションを監視し、TP/SLトリガーをチェック (レートリミットに注意)
            await position_monitor_and_update_sltp()
        await asyncio.sleep(MONITOR_INTERVAL) # 10秒ごとに監視


# ====================================================================================
# FASTAPI SETUP
# ====================================================================================

app = FastAPI()

@app.get("/")
async def root():
    return {"message": f"Apex BOT {BOT_VERSION} is running."}

# UptimeRobotなどの監視サービスからのヘルスチェックに対応
@app.head("/")
async def head_root(request: Request):
    # ヘルスチェックに成功したと見なす
    return Response(status_code=200)


@app.on_event("startup")
async def startup_event():
    """アプリケーション起動時に実行"""
    logging.info("BOTサービスを開始しました。")
    
    # スケジューラをバックグラウンドで開始
    asyncio.create_task(main_bot_scheduler())
    asyncio.create_task(position_monitor_scheduler())


# エラーハンドラ 
@app.exception_handler(Exception)
async def default_exception_handler(request, exc):
    """捕捉されなかった例外を処理し、ログに記録する"""
    
    if "Unclosed" not in str(exc):
        logging.error(f"❌ 未処理の致命的なエラーが発生しました: {exc}")
        # Telegram通知はループ内で実行されるため、ここでは省略
        
    return JSONResponse(
        status_code=500,
        content={"message": "Internal Server Error", "detail": str(exc)},
    )
    
if __name__ == '__main__':
    # サーバーの起動 (開発/本番環境に応じてホスト/ポートを変更)
    # 環境変数からポートを取得し、デフォルトは8000
    port = int(os.getenv("PORT", 8000)) 
    uvicorn.run("main_render (26):app", host="0.0.0.0", port=port, reload=False) # reload=False for production
