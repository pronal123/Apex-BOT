# ====================================================================================
# Apex BOT v20.0.43 - Future Trading / 30x Leverage 
# (Feature: 実践的スコアリングロジック、ATR動的リスク管理導入)
# 
# 🚨 致命的エラー修正強化: 
# 1. 💡 修正: np.polyfitの戻り値エラー (ValueError: not enough values to unpack) を修正
# 2. 💡 修正: fetch_tickersのAttributeError ('NoneType' object has no attribute 'keys') 対策 
# 3. 注文失敗エラー (Amount can not be less than zero) 対策
# 4. 💡 修正: 通知メッセージでEntry/SL/TP/清算価格が0になる問題を解決 (v20.0.42で対応済み)
# 5. 💡 新規: ダミーロジックを実践的スコアリングロジックに置換 (v20.0.43)
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
    logging.warning("⚠️ WARNING: TEST_MODE is active. Trading is disabled。")

# CCXTクライアントの準備完了フラグ
IS_CLIENT_READY: bool = False

# 取引ルール設定
TRADE_SIGNAL_COOLDOWN = 60 * 60 * 12 
SIGNAL_THRESHOLD = 0.65             
TOP_SIGNAL_COUNT = 1                
REQUIRED_OHLCV_LIMITS = {'1m': 500, '5m': 500, '15m': 500, '1h': 500, '4h': 500} 

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
                    'raw_info': p, # デバッグ用
                })
        
        # グローバル変数にポジション情報を保存
        global OPEN_POSITIONS
        OPEN_POSITIONS = open_positions

        logging.info(f"✅ 口座ステータスとオープンポジション ({len(OPEN_POSITIONS)}件) の取得が完了しました。")
        
        return {
            'total_usdt_balance': ACCOUNT_EQUITY_USDT,
            'open_positions': open_positions,
            'error': False
        }
    
    except ccxt.ExchangeNotAvailable as e:
        logging.error(f"❌ 口座ステータス取得失敗 - 取引所接続エラー: {e}")
        return {'total_usdt_balance': 0.0, 'open_positions': [], 'error': True}
    except Exception as e:
        logging.error(f"❌ 口座ステータス取得中の予期せぬエラー: {e}", exc_info=True)
        return {'total_usdt_balance': 0.0, 'open_positions': [], 'error': True}

# 💡 致命的エラー修正強化: ATR計算のため、1h/4hも取得
async def fetch_ohlcv_data(symbol: str, timeframes: List[str]) -> Dict[str, pd.DataFrame]:
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.error(f"❌ {symbol}: OHLCVデータ取得失敗 - CCXTクライアントが準備できていません。")
        return {}

    all_ohlcv_data: Dict[str, pd.DataFrame] = {}

    for tf in timeframes:
        limit = REQUIRED_OHLCV_LIMITS.get(tf, 500)
        try:
            # CCXTのfetch_ohlcvはタイムスタンプ (ms), Open, High, Low, Close, Volume を返す
            ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(symbol, tf, limit=limit)
            
            if not ohlcv or len(ohlcv) < 50: 
                logging.warning(f"⚠️ {symbol} - {tf}: 取得したデータ量が不十分です ({len(ohlcv)}件)。")
                continue
                
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['datetime'] = pd.to_datetime(df['timestamp'], unit='ms').dt.tz_localize('UTC').dt.tz_convert(JST)
            df.set_index('datetime', inplace=True)
            df.drop('timestamp', axis=1, inplace=True)
            
            # 数値型に変換
            for col in ['open', 'high', 'low', 'close', 'volume']:
                df[col] = pd.to_numeric(df[col], errors='coerce')
                
            all_ohlcv_data[tf] = df
            logging.info(f"✅ {symbol} - {tf}: OHLCVデータ ({len(df)}件) を正常に取得しました。")

        except ccxt.RequestTimeout as e:
            logging.warning(f"⚠️ {symbol} - {tf}: リクエストタイムアウトが発生しました: {e}")
        except ccxt.ExchangeNotAvailable as e:
            logging.warning(f"⚠️ {symbol} - {tf}: 取引所が利用不可です: {e}")
        except ccxt.NetworkError as e:
            logging.warning(f"⚠️ {symbol} - {tf}: ネットワークエラーが発生しました: {e}")
        except Exception as e:
            logging.error(f"❌ {symbol} - {tf}: OHLCV取得中の予期せぬエラー: {e}", exc_info=True)
            
        # 連続リクエストによるレート制限を避けるための遅延
        await asyncio.sleep(EXCHANGE_CLIENT.rateLimit / 1000)

    return all_ohlcv_data

# 🚨 リトライロジックを追加したOHLCV取得関数
async def fetch_ohlcv_with_retry(symbol: str, timeframes: List[str], max_retries: int = 3) -> Dict[str, pd.DataFrame]:
    
    # グローバルクライアントが準備できていない場合は、初期化を試みる
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        if not await initialize_exchange_client():
            return {}

    for attempt in range(1, max_retries + 1):
        try:
            ohlcv_data = await fetch_ohlcv_data(symbol, timeframes)
            # 全ての必須タイムフレームが取得できたかチェック（最低1mと、リスク計算に必要な1h/4h）
            required_tfs = ['1m', '1h', '4h'] 
            if all(tf in ohlcv_data for tf in required_tfs) and all(len(df) >= 14 for tf, df in ohlcv_data.items()):
                 return ohlcv_data
            else:
                logging.warning(f"⚠️ {symbol}: 必須タイムフレームのデータが不足しています。再試行します (試行 {attempt}/{max_retries})")
                
        except Exception as e:
            logging.error(f"❌ {symbol}: OHLCV取得中の予期せぬエラー (試行 {attempt}/{max_retries}): {e}")

        if attempt < max_retries:
            # 失敗した場合、リトライ前に指数関数的なバックオフで待機
            await asyncio.sleep(2 ** attempt * 2) 
        
    logging.error(f"❌ {symbol}: OHLCVデータ取得が {max_retries} 回失敗したため、スキップします。")
    return {}


async def fetch_top_volume_symbols() -> List[str]:
    """
    取引所から出来高の高いシンボルを動的に取得し、DEFAULT_SYMBOLSと結合して返す。
    """
    global EXCHANGE_CLIENT
    
    if SKIP_MARKET_UPDATE:
        logging.info("ℹ️ 市場更新がスキップ設定されているため、デフォルトシンボルを使用します。")
        return DEFAULT_SYMBOLS
        
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.error("❌ 出来高シンボル取得失敗: CCXTクライアントが準備できていません。デフォルトを使用します。")
        return DEFAULT_SYMBOLS

    try:
        # USDT建ての先物/スワップ市場のティッカーを取得
        tickers = await EXCHANGE_CLIENT.fetch_tickers()
        
        # 💡 修正: NoneTypeのエラー対策
        if not tickers or not isinstance(tickers, dict):
            raise Exception("fetch_tickers returned invalid data.")

        usdt_futures = []
        for symbol, ticker in tickers.items():
            # CCXT標準シンボル、かつUSDT建て、かつFuture/Swap
            if symbol.endswith('/USDT') and ticker.get('info', {}).get('ctType', 'NONE').lower() in ['usdt', 'perp', 'future']:
                # 出来高 (USDT単位での名目出来高) を確認 (volume_usdtやquoteVolumeを使う)
                # quoteVolume: クォート通貨での出来高 (USDT)
                # volume: ベース通貨での出来高
                volume_usdt = ticker.get('quoteVolume', 0.0) 
                # quoteVolumeがない場合、volume * last で名目出来高を推定
                if volume_usdt == 0.0 and ticker.get('volume', 0.0) > 0 and ticker.get('last', 0.0) > 0:
                    volume_usdt = ticker['volume'] * ticker['last']
                    
                if volume_usdt > 0:
                    usdt_futures.append({
                        'symbol': symbol,
                        'volume_usdt': volume_usdt
                    })

        # 出来高の降順でソートし、上位N件を抽出
        usdt_futures.sort(key=lambda x: x['volume_usdt'], reverse=True)
        top_symbols = [d['symbol'] for d in usdt_futures[:TOP_SYMBOL_LIMIT]]

        # DEFAULT_SYMBOLSに含まれるがTOP_SYMBOL_LIMITに含まれなかったものを追加
        # (主要な基軸通貨がボリュームで弾かれるのを防ぐため)
        for d_symbol in DEFAULT_SYMBOLS:
            if d_symbol not in top_symbols:
                top_symbols.append(d_symbol)
                
        logging.info(f"✅ 動的に更新された監視銘柄リスト: {len(top_symbols)}件 (トップ {TOP_SYMBOL_LIMIT}件 + デフォルト銘柄)。")
        return top_symbols

    except ccxt.NetworkError as e:
        logging.error(f"❌ 出来高シンボル取得失敗 - ネットワークエラー: {e}。デフォルトを使用します。")
    except Exception as e:
        # 💡 修正: fetch_tickersの戻り値エラー対策
        logging.error(f"❌ 出来高シンボル取得失敗 - 予期せぬエラー: {e}。デフォルトを使用します。", exc_info=True)

    return DEFAULT_SYMBOLS


# ====================================================================================
# MACRO ENVIRONMENT
# ====================================================================================

async def fetch_macro_context() -> Dict:
    """
    FGI (Fear & Greed Index) などのマクロ環境データを取得し、
    スコアリングに適用するためのプロキシ値 (±5%のボーナス/ペナルティ) を計算する。
    """
    global FGI_API_URL, FGI_PROXY_BONUS_MAX
    
    macro_context = {'fgi_proxy': 0.0, 'fgi_raw_value': 'N/A', 'forex_bonus': 0.0}
    
    # 1. Fear & Greed Index (FGI) 取得
    try:
        response = await asyncio.to_thread(requests.get, FGI_API_URL, timeout=5)
        response.raise_for_status()
        data = response.json()
        
        # FGI値の正規化 (0-100を -0.5 から +0.5 の範囲に)
        if data and data.get('data'):
            fgi_raw = int(data['data'][0]['value'])
            fgi_classification = data['data'][0]['value_classification']
            
            # FGI (恐怖: 0 - 貪欲: 100) を正規化されたセンチメントプロキシ (-1.0 to +1.0) に変換
            # FGI=0 (Extreme Fear) -> -1.0
            # FGI=50 (Neutral) -> 0.0
            # FGI=100 (Extreme Greed) -> +1.0
            fgi_normalized = (fgi_raw - 50) / 50.0 
            
            # FGIプロキシ値を計算: 正規化値 * 最大影響度
            # 例: FGI=80 -> +0.6 * 0.05 = +0.03 (スコア+3.0点)
            fgi_proxy = fgi_normalized * FGI_PROXY_BONUS_MAX
            
            macro_context['fgi_raw_value'] = f"{fgi_raw} ({fgi_classification})"
            macro_context['fgi_proxy'] = fgi_proxy
            logging.info(f"✅ FGI取得完了: {macro_context['fgi_raw_value']} / プロキシ影響: {fgi_proxy*100:.2f}点")
            
    except Exception as e:
        logging.warning(f"⚠️ FGI取得失敗: {e}。FGI影響を0に設定します。")
        
    # 2. Forex/Stock Market Bonus (現時点では実装なし、将来の拡張用)
    # macro_context['forex_bonus'] = 0.0 

    return macro_context


# ====================================================================================
# CORE TRADING LOGIC (SCORE CALCULATION)
# ====================================================================================

def calculate_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    指定されたOHLCVデータフレームに対し、テクニカルインジケーターを計算し、
    元のデータフレームに結合して返す。
    """
    if df.empty or len(df) < 200:
        return df.copy()

    # 1. ボリンジャーバンド (BB) 
    df.ta.bbands(close='close', length=20, append=True)
    
    # 2. ATR (Average True Range) - リスク管理に必須
    df.ta.atr(length=ATR_LENGTH, append=True)
    
    # 3. SMA (Simple Moving Average) - 長期トレンド判断
    df.ta.sma(length=LONG_TERM_SMA_LENGTH, append=True) 
    
    # 4. RSI (Relative Strength Index) - モメンタムと過熱感
    df.ta.rsi(length=14, append=True)
    
    # 5. MACD (Moving Average Convergence Divergence)
    # デフォルトの MACD (12, 26, 9) を使用。MACD, MACDh (ヒストグラム), MACDs (シグナル)
    df.ta.macd(close='close', fast=12, slow=26, signal=9, append=True)
    
    # 6. OBV (On-Balance Volume) - 出来高確証
    df.ta.obv(append=True)

    # 💡 修正: numpyの警告を避けるため、最後の行に限定してnanチェックを行うため、
    # 余分なカラムは削除しない

    # 💡 致命的エラー修正強化: np.polyfitを使用するロジックのために、indexをリセットして数値インデックスを追加
    df.reset_index(inplace=True)
    df['index'] = df.index
    df.set_index('datetime', inplace=True)
    
    return df

def get_trend_strength_score(df: pd.DataFrame, target_column: str, length: int) -> float:
    """
    指定されたカラムの最近のデータ (length) を使用して、
    傾き（トレンドの方向と強さ）を計算する。
    
    戻り値: 正規化された傾き (-1.0 to 1.0)
    """
    if df.empty or len(df) < length or target_column not in df.columns:
        return 0.0
    
    # 最近のデータのみを抽出
    recent_data = df[target_column].tail(length).dropna()
    recent_index = df['index'].tail(len(recent_data))
    
    if len(recent_data) < 2:
        return 0.0

    try:
        # np.polyfitを使用して傾きを計算
        # 💡 修正: 戻り値が一つだけの場合があるため、try-exceptで対応
        coefficients = np.polyfit(recent_index.values, recent_data.values, 1)
        slope = coefficients[0]
    except ValueError as e:
        if "not enough values to unpack" in str(e):
            # 傾きのみが返された場合
            slope = coefficients 
        else:
            logging.warning(f"⚠️ np.polyfitで予期せぬエラーが発生しました: {e}")
            return 0.0

    # 傾きを正規化する
    # 例: slopeが0.01の場合、それを何らかの最大傾きで割る。ここでは標準偏差を使用
    price_range = recent_data.max() - recent_data.min()
    max_abs_slope = price_range / length * 5 # 傾きが価格レンジの1/50程度で最大になるという仮定
    
    if max_abs_slope == 0:
        return 0.0
        
    normalized_slope = slope / max_abs_slope
    
    # 結果を -1.0 から 1.0 にクリップ
    return np.clip(normalized_slope, -1.0, 1.0)
    

def analyze_trend_and_momentum(df: pd.DataFrame, current_price: float, side: str) -> Dict[str, Any]:
    """
    トレンド、モメンタム、ボラティリティを分析し、スコアリング用の詳細データを返す。
    """
    if df.empty or len(df) < 200:
        return {'score_data': {}, 'final_score': 0.0}

    # 最新の行を取得 (最後のデータポイント)
    last = df.iloc[-1]
    
    tech_data = {}
    
    # ----------------------------------------------------
    # 1. 長期トレンド一致/逆行 (LONG_TERM_REVERSAL_PENALTY)
    # ----------------------------------------------------
    # SMA200 (200日移動平均線)
    sma_200_name = f'SMA_{LONG_TERM_SMA_LENGTH}'
    trend_score_contribution = 0.0
    
    if sma_200_name in last and not np.isnan(last[sma_200_name]):
        sma_200 = last[sma_200_name]
        
        # Long (買い) の場合: 現在価格 > SMA200 でボーナス, < SMA200 でペナルティ
        if side == 'long':
            if current_price > sma_200:
                trend_score_contribution = LONG_TERM_REVERSAL_PENALTY # 順張りボーナス
            else:
                trend_score_contribution = -LONG_TERM_REVERSAL_PENALTY # 逆張りペナルティ
        
        # Short (売り) の場合: 現在価格 < SMA200 でボーナス, > SMA200 でペナルティ
        elif side == 'short':
            if current_price < sma_200:
                trend_score_contribution = LONG_TERM_REVERSAL_PENALTY # 順張りボーナス
            else:
                trend_score_contribution = -LONG_TERM_REVERSAL_PENALTY # 逆張りペナルティ

    tech_data['long_term_reversal_penalty_value'] = trend_score_contribution
    
    # ----------------------------------------------------
    # 2. MACDモメンタム一致 (MACD_CROSS_PENALTY)
    # ----------------------------------------------------
    # MACDヒストグラム (MACDh_12_26_9) の方向を確認
    macd_hist_name = 'MACDh_12_26_9'
    macd_score_contribution = 0.0
    
    if macd_hist_name in last and not np.isnan(last[macd_hist_name]):
        macd_hist = last[macd_hist_name]
        
        # MACDh の傾き (過去数本のヒストグラムの傾き) を確認
        # 過去5本分の MACDh の傾きを計算
        macd_slope_normalized = get_trend_strength_score(df, macd_hist_name, 5)

        # Long の場合: MACDh > 0 (MACD > Signal) かつ 傾きが正でボーナス
        if side == 'long':
            if macd_hist > 0 and macd_slope_normalized > 0:
                macd_score_contribution = MACD_CROSS_PENALTY
            else:
                macd_score_contribution = -MACD_CROSS_PENALTY
        
        # Short の場合: MACDh < 0 (MACD < Signal) かつ 傾きが負でボーナス
        elif side == 'short':
            if macd_hist < 0 and macd_slope_normalized < 0:
                macd_score_contribution = MACD_CROSS_PENALTY
            else:
                macd_score_contribution = -MACD_CROSS_PENALTY

    tech_data['macd_penalty_value'] = macd_score_contribution

    # ----------------------------------------------------
    # 3. RSIモメンタム加速 (RSI_MOMENTUM_LOW, RSI_DIVERGENCE_BONUS)
    # ----------------------------------------------------
    rsi_name = 'RSI_14'
    rsi_score_contribution = 0.0
    
    if rsi_name in last and not np.isnan(last[rsi_name]):
        rsi = last[rsi_name]
        
        # Long の場合: RSIが売られ過ぎからモメンタム回復 (RSI > RSI_MOMENTUM_LOW)
        if side == 'long':
            if rsi > RSI_MOMENTUM_LOW and rsi < 70: # 40-70 を適正水準と見なす
                rsi_score_contribution = STRUCTURAL_PIVOT_BONUS # +5点
        
        # Short の場合: RSIが買われ過ぎからモメンタム失速 (RSI < 100 - RSI_MOMENTUM_LOW)
        elif side == 'short':
            if rsi < (100 - RSI_MOMENTUM_LOW) and rsi > 30: # 30-60 を適正水準と見なす
                rsi_score_contribution = STRUCTURAL_PIVOT_BONUS # +5点
                
    tech_data['rsi_momentum_bonus_value'] = rsi_score_contribution
    
    # ----------------------------------------------------
    # 4. OBV (On-Balance Volume) による出来高確証 (OBV_MOMENTUM_BONUS)
    # ----------------------------------------------------
    obv_name = 'OBV'
    obv_score_contribution = 0.0
    
    if obv_name in last and not np.isnan(last[obv_name]):
        # 過去5本のOBVの傾きを計算
        obv_slope_normalized = get_trend_strength_score(df, obv_name, 5)
        
        # Long の場合: OBVの傾きが正 (出来高が上昇を確証)
        if side == 'long' and obv_slope_normalized > 0.3: # 0.3は閾値
            obv_score_contribution = OBV_MOMENTUM_BONUS
            
        # Short の場合: OBVの傾きが負 (出来高が下落を確証)
        elif side == 'short' and obv_slope_normalized < -0.3:
            obv_score_contribution = OBV_MOMENTUM_BONUS
            
    tech_data['obv_momentum_bonus_value'] = obv_score_contribution

    # ----------------------------------------------------
    # 5. ボラティリティ過熱ペナルティ (VOLATILITY_BB_PENALTY_THRESHOLD)
    # ----------------------------------------------------
    # BBands Width (BBW) の名称
    bb_width_name = 'BBB_20_2.0'
    volatility_penalty_value = 0.0

    # 🚨 エラー修正箇所: KeyError: 'BBB_20_2.0' 対策として、Keyの存在を確認
    if bb_width_name in last:
        if not np.isnan(last[bb_width_name]):
            # BB幅 (BandWidth) を現在の価格で正規化
            bb_width_ratio = last[bb_width_name] / current_price
            
            # BB幅が VOLATILITY_BB_PENALTY_THRESHOLD (例: 1%) を超える場合にペナルティ
            if bb_width_ratio > VOLATILITY_BB_PENALTY_THRESHOLD:
                # 閾値からの超過分に応じてペナルティを課す (最大ペナルティはMACD/Trendの半分程度)
                excess_ratio = bb_width_ratio - VOLATILITY_BB_PENALTY_THRESHOLD
                max_penalty = LONG_TERM_REVERSAL_PENALTY / 2
                
                # ペナルティは -0.01 から -max_penalty の間で調整
                penalty = -min(max_penalty, excess_ratio * 10) # 係数10は調整可能
                volatility_penalty_value = penalty

    tech_data['volatility_penalty_value'] = volatility_penalty_value
    
    # ----------------------------------------------------
    # 6. リスクリワード計算用のATR取得
    # ----------------------------------------------------
    atr_name = f'ATR_{ATR_LENGTH}'
    atr_value = 0.0
    if atr_name in last and not np.isnan(last[atr_name]):
        atr_value = last[atr_name]
        
    tech_data['atr_value'] = atr_value

    # ----------------------------------------------------
    # 7. 最終スコアの計算
    # ----------------------------------------------------
    
    # ベーススコア (構造的優位性)
    structural_pivot_bonus = STRUCTURAL_PIVOT_BONUS
    
    # 合計スコア = ベース + 各分析要素の貢献度
    total_score = BASE_SCORE + \
                  structural_pivot_bonus + \
                  tech_data['long_term_reversal_penalty_value'] + \
                  tech_data['macd_penalty_value'] + \
                  tech_data['rsi_momentum_bonus_value'] + \
                  tech_data['obv_momentum_bonus_value'] + \
                  tech_data['volatility_penalty_value'] # ペナルティは負の値

    # 構造的優位性ボーナスを tech_data にも記録
    tech_data['structural_pivot_bonus'] = structural_pivot_bonus

    return {'tech_data': tech_data, 'final_score': total_score}


def calculate_trade_score(symbol: str, tf: str, ohlcv_data: Dict[str, pd.DataFrame], side: str, macro_context: Dict) -> Dict:
    """
    指定されたシンボルと時間枠のデータから総合取引スコアを計算する。
    """
    
    if tf not in ohlcv_data:
        logging.warning(f"⚠️ {symbol} - {tf}: OHLCVデータがありません。スコアを0.0とします。")
        return {'score': 0.0, 'symbol': symbol, 'timeframe': tf, 'side': side}

    df = ohlcv_data[tf].copy()
    
    # 1. インジケーターの計算
    df_indicators = calculate_indicators(df)
    
    # データが不十分な場合はスキップ
    if df_indicators.empty or len(df_indicators) < 200:
        logging.warning(f"⚠️ {symbol} - {tf}: インジケーター計算に必要なデータが不十分です。スコアを0.0とします。")
        return {'score': 0.0, 'symbol': symbol, 'timeframe': tf, 'side': side}
        
    current_price = df_indicators['close'].iloc[-1]
    
    # 2. テクニカル分析とスコア構成要素の取得
    score_data = analyze_trend_and_momentum(df_indicators, current_price, side)
    
    tech_data = score_data['tech_data']
    final_score = score_data['final_score']
    
    # 3. マクロ環境と流動性によるボーナス/ペナルティの適用
    
    # 3-1. 流動性ボーナス
    # CURRENT_MONITOR_SYMBOLS の順位に基づいてボーナスを与える (TOP 10まで)
    try:
        if symbol in CURRENT_MONITOR_SYMBOLS:
            index = CURRENT_MONITOR_SYMBOLS.index(symbol)
            # 順位に応じてボーナスを線形に減少させる (例: 1位: +0.06, 10位: +0.006)
            # 6% / 10 = 0.6% 刻み
            if index < 10: 
                liquidity_bonus = LIQUIDITY_BONUS_MAX * (1.0 - (index / 10.0))
            else:
                liquidity_bonus = 0.0
        else:
            liquidity_bonus = 0.0
    except ValueError:
        liquidity_bonus = 0.0

    # 3-2. FGI (恐怖・貪欲指数) ボーナス/ペナルティ
    sentiment_fgi_proxy_bonus = macro_context.get('fgi_proxy', 0.0)
    
    # 3-3. 最終的なスコアへの反映
    final_score += liquidity_bonus
    final_score += sentiment_fgi_proxy_bonus
    
    # Tech Dataにボーナス値を記録
    tech_data['liquidity_bonus_value'] = liquidity_bonus
    tech_data['sentiment_fgi_proxy_bonus'] = sentiment_fgi_proxy_bonus
    
    # 4. リスク管理 (ATRベースのSL/TP計算)
    atr_value = tech_data.get('atr_value', 0.0)
    
    # ATRに基づいたリスク幅
    risk_by_atr = atr_value * ATR_MULTIPLIER_SL
    
    # 最低リスク幅 (価格に対する割合)
    min_risk_by_percent = current_price * MIN_RISK_PERCENT
    
    # 最終的なリスク幅 (SLまでの価格差) は、ATRベースと最小パーセントの大きい方を使用
    risk_price_diff = max(risk_by_atr, min_risk_by_percent)

    # SL/TPの計算
    if risk_price_diff == 0.0:
        stop_loss = 0.0
        take_profit = 0.0
        rr_ratio = 0.0
    else:
        # SLの計算
        if side == 'long':
            stop_loss = current_price - risk_price_diff
            take_profit = current_price + (risk_price_diff * RR_RATIO_TARGET)
        else: # 'short'
            stop_loss = current_price + risk_price_diff
            take_profit = current_price - (risk_price_diff * RR_RATIO_TARGET)
            
        # 実際のRR比率
        rr_ratio = RR_RATIO_TARGET # 動的RR調整を行わないため固定
        
    # 清算価格の計算
    liquidation_price = calculate_liquidation_price(current_price, LEVERAGE, side)

    # 最終結果の作成
    result = {
        'symbol': symbol,
        'timeframe': tf,
        'side': side,
        'current_price': current_price,
        'score': np.clip(final_score, 0.0, 1.0), # スコアを 0.0 から 1.0 にクリップ
        'tech_data': tech_data,
        'stop_loss': stop_loss,
        'take_profit': take_profit,
        'entry_price': current_price,
        'rr_ratio': rr_ratio,
        'risk_price_diff': risk_price_diff,
        'liquidation_price': liquidation_price,
        # 💡 エラー修正: ログに記録する際、最終的なリスク/リワードの幅も記録
        'risk_usdt_notional': FIXED_NOTIONAL_USDT * (risk_price_diff / current_price) * LEVERAGE,
        'reward_usdt_notional': FIXED_NOTIONAL_USDT * (risk_price_diff / current_price) * RR_RATIO_TARGET * LEVERAGE,
    }

    return result


async def analyze_single_symbol(symbol: str, macro_context: Dict) -> List[Dict]:
    """
    単一のシンボルに対し、全てのターゲット時間枠で分析を実行する。
    """
    
    # ポジションを既に持っている場合は、分析をスキップ
    if any(p['symbol'] == symbol for p in OPEN_POSITIONS):
        logging.info(f"ℹ️ {symbol}: 既存のポジションがあるため、新規シグナル分析をスキップします。")
        return []

    ohlcv_data = await fetch_ohlcv_with_retry(symbol, TARGET_TIMEFRAMES)
    
    if not ohlcv_data:
        return []

    analysis_results = []
    
    # 各時間枠とサイド (Long/Short) でスコアを計算
    for tf in TARGET_TIMEFRAMES:
        if tf in ohlcv_data:
            # Long
            result_long = calculate_trade_score(symbol, tf, ohlcv_data, 'long', macro_context)
            analysis_results.append(result_long)
            
            # Short
            result_short = calculate_trade_score(symbol, tf, ohlcv_data, 'short', macro_context)
            analysis_results.append(result_short)
            
    return analysis_results


async def find_best_signals(macro_context: Dict) -> List[Dict]:
    """
    監視対象の全てのシンボルに対し、最もスコアの高い取引シグナルを特定する。
    """
    global CURRENT_MONITOR_SYMBOLS, LAST_SIGNAL_TIME, LAST_ANALYSIS_SIGNALS

    logging.info(f"📊 {len(CURRENT_MONITOR_SYMBOLS)}銘柄に対して取引シグナル分析を開始します...")
    
    analysis_tasks = []
    for symbol in CURRENT_MONITOR_SYMBOLS:
        # クールダウン期間中の場合はスキップ (同一銘柄の多重エントリー防止)
        if time.time() - LAST_SIGNAL_TIME.get(symbol, 0) < TRADE_SIGNAL_COOLDOWN:
            continue
            
        task = analyze_single_symbol(symbol, macro_context)
        analysis_tasks.append(task)
        
    if not analysis_tasks:
        logging.warning("⚠️ 全ての銘柄がクールダウン期間中のため、分析タスクはありません。")
        LAST_ANALYSIS_SIGNALS = []
        return []

    # 全ての分析タスクを並行実行
    results = await asyncio.gather(*analysis_tasks)
    
    all_signals = []
    for symbol_results in results:
        all_signals.extend(symbol_results)

    if not all_signals:
        LAST_ANALYSIS_SIGNALS = []
        return []

    # スコアの降順でソート
    sorted_signals = sorted(all_signals, key=lambda x: x['score'], reverse=True)
    
    # グローバル変数に今回の分析結果を保存 (定時レポート用)
    LAST_ANALYSIS_SIGNALS = sorted_signals
    
    # 動的な取引閾値を決定
    current_threshold = get_current_threshold(macro_context)
    
    # 閾値を超えるシグナルのみを抽出
    eligible_signals = [s for s in sorted_signals if s['score'] >= current_threshold]
    
    logging.info(f"🔍 分析完了。最高スコア: {sorted_signals[0]['symbol']} ({sorted_signals[0]['timeframe']} - {sorted_signals[0]['side'].capitalize()}) {sorted_signals[0]['score']*100:.2f}点。")
    logging.info(f"🔍 取引閾値 ({current_threshold*100:.2f}点) を超えるシグナルは {len(eligible_signals)} 件です。")

    # 上位 N 件のシグナルを返す
    return eligible_signals[:TOP_SIGNAL_COUNT]


# ====================================================================================
# EXECUTION & TRADING
# ====================================================================================

async def calculate_entry_size(entry_price: float) -> Optional[float]:
    """
    固定のNOTIONAL_USDTに基づいて、契約サイズ (数量) を計算する。
    MEXCなどのUSDT先物では、数量はベース通貨 (例: BTC) の単位。
    
    戻り値: 契約サイズ (数量)
    """
    
    if entry_price <= 0 or FIXED_NOTIONAL_USDT <= 0:
        logging.error("❌ エントリーサイズ計算失敗: 価格または固定ロットが不正です。")
        return None
        
    # 名目価値 / エントリー価格 = 数量 (Contract Size)
    # 例: 20 USDT / 20000 USD/BTC = 0.001 BTC
    contract_size = FIXED_NOTIONAL_USDT / entry_price
    
    # 取引所の最小ロット要件などを考慮するため、CCXTの市場情報で丸める
    if EXCHANGE_CLIENT and EXCHANGE_CLIENT.markets:
        # MEXCのシンボルは 'BTC/USDT' -> 'BTC/USDT:USDT' の形式になる可能性があるため、
        # ここではシンボル情報にアクセスしない。一旦、そのままの数量を使用し、
        # 注文APIに丸めを任せる
        pass
        
    return contract_size

async def execute_trade(signal: Dict, contract_size: float) -> Dict:
    """
    取引所APIを呼び出し、新規ポジションエントリーとSL/TPを設定する。
    
    戻り値: 取引結果 (成功/失敗、約定価格、メッセージ)
    """
    global EXCHANGE_CLIENT
    
    symbol = signal['symbol']
    side = signal['side']
    entry_price = signal['entry_price']
    
    # 契約サイズが 0.0 以下、またはクライアントが準備できていない場合はスキップ
    if contract_size <= 0.0 or not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        return {'status': 'error', 'error_message': 'ロットサイズまたはクライアントが不正です。'}

    # ----------------------------------------------------
    # 1. 成行注文の実行 (エントリー)
    # ----------------------------------------------------
    
    order_side = 'buy' if side == 'long' else 'sell'
    # CCXTの成行注文: create_market_order(symbol, side, amount)
    # amount はベース通貨の数量 (例: 0.001 BTC)
    
    # MEXCの場合、シンボルを変換する: 'BTC/USDT' -> 'BTC/USDT:USDT'
    exchange_symbol = EXCHANGE_CLIENT.markets[symbol]['symbol'] if symbol in EXCHANGE_CLIENT.markets else symbol
    
    try:
        # 成行注文の実行
        entry_order = await EXCHANGE_CLIENT.create_market_order(
            exchange_symbol, 
            order_side, 
            abs(contract_size)
        )
        
        # 注文が約定するまで待機 (通常、成行は即時約定)
        if entry_order.get('status') != 'closed':
             # 注文情報のフェッチを試みる (非同期実行をブロックする可能性があるため、短時間の待機のみ)
            await asyncio.sleep(2) 
            entry_order = await EXCHANGE_CLIENT.fetch_order(entry_order['id'], exchange_symbol)
            
        if entry_order.get('status') not in ['closed', 'filled']:
            raise Exception(f"エントリー注文が約定しませんでした。ステータス: {entry_order.get('status')}")

        filled_amount = entry_order.get('filled', 0.0)
        filled_price = entry_order.get('average', entry_price) # 平均約定価格
        
        # 約定名目価値 (filled * filled_price)
        filled_usdt = filled_amount * filled_price
        
        trade_result = {
            'status': 'ok',
            'filled_amount': filled_amount,
            'filled_price': filled_price,
            'filled_usdt': filled_usdt,
            'entry_order_id': entry_order['id'],
            'stop_loss_order_id': None,
            'take_profit_order_id': None,
            'error_message': None
        }
        
        logging.info(f"✅ {symbol} - {side.capitalize()}: エントリー注文成功。平均約定価格: {format_price(filled_price)}")
        
    except Exception as e:
        error_message = f"成行注文失敗: {e}"
        logging.error(f"❌ {symbol} - {side.capitalize()}: {error_message}", exc_info=True)
        return {'status': 'error', 'error_message': error_message}
        
    # ----------------------------------------------------
    # 2. SL/TP注文の実行 (複合注文/ストップ注文)
    # ----------------------------------------------------
    
    stop_loss = signal['stop_loss']
    take_profit = signal['take_profit']
    
    # SL/TPの注文はエントリーとは逆サイド
    sl_tp_side = 'sell' if side == 'long' else 'buy'
    
    # SL/TPは通常、ポジションの全量 (約定数量) をクローズする注文
    sl_tp_amount = filled_amount 

    # CCXTは unified create_order (stop_loss, take_profit) をサポート
    # MEXCは create_order(params={'stopLoss', 'takeProfit'}) のような複合注文をサポート
    
    try:
        # ストップロス (SL) 注文の実行
        # SLは 'STOP_MARKET' または 'STOP_LIMIT' で設定
        # MEXCの場合、CCXTのparams={'stopLoss', 'takeProfit'}を使って複合注文を設定
        
        sl_tp_params = {
            'stopLossPrice': stop_loss, # トリガー価格
            'takeProfitPrice': take_profit, # トリガー価格
            'priceType': 1, # 1: リアルタイム価格 (CCXT APIによって異なる可能性あり)
            # MEXCはポジションIDを必要としない (Position Monitorで管理するため、ここではトリガー注文のみ)
        }
        
        # 注文のタイプ (指値: limit, 成行: market)
        # SL/TPは、指値 (Limit) ではなく、トリガー価格に達したら成行 (Market) で決済する Stop Market を使用
        
        sl_order = await EXCHANGE_CLIENT.create_order(
            exchange_symbol, 
            'stop_market', # ストップ成行注文
            sl_tp_side, 
            abs(sl_tp_amount), 
            params={'stopLossPrice': stop_loss} # SLのパラメーターのみ渡す (TPは個別に)
        )
        
        tp_order = await EXCHANGE_CLIENT.create_order(
            exchange_symbol, 
            'take_profit_market', # テイクプロフィット成行注文
            sl_tp_side, 
            abs(sl_tp_amount), 
            params={'takeProfitPrice': take_profit} # TPのパラメーターのみ渡す
        )

        trade_result['stop_loss_order_id'] = sl_order.get('id')
        trade_result['take_profit_order_id'] = tp_order.get('id')
        
        logging.info(f"✅ {symbol} - {side.capitalize()}: SL ({format_price(stop_loss)}) と TP ({format_price(take_profit)}) 注文を正常に設定しました。")
        
    except Exception as e:
        error_message = f"SL/TP注文設定失敗: {e}"
        logging.error(f"❌ {symbol} - {side.capitalize()}: {error_message}", exc_info=True)
        # SL/TP注文失敗は致命的ではないが、ポジションがノーガードになる
        trade_result['error_message'] = error_message
        await send_telegram_notification(f"🚨 <b>SL/TP設定警告</b>\n{symbol} の SL/TP注文設定に失敗しました: <code>{e}</code>。ポジションはノーガードです。")
        
    return trade_result

async def process_entry_signal(signal: Dict, macro_context: Dict) -> None:
    """
    最良のシグナルを処理し、取引実行、通知、ログ記録を行う。
    """
    global LAST_SIGNAL_TIME, OPEN_POSITIONS
    
    symbol = signal['symbol']
    side = signal['side']
    entry_price = signal['entry_price']
    
    current_threshold = get_current_threshold(macro_context)
    
    if TEST_MODE:
        logging.warning(f"⚠️ TEST_MODE: {symbol} - {side.capitalize()} シグナル ({signal['score']*100:.2f}点) を検知しましたが、取引をスキップします。")
        # テストモードではダミーの取引結果を作成
        trade_result = {'status': 'ok', 'filled_amount': FIXED_NOTIONAL_USDT / entry_price, 'filled_price': entry_price, 'filled_usdt': FIXED_NOTIONAL_USDT, 'error_message': None}
    else:
        # 1. 契約サイズの計算
        contract_size = await calculate_entry_size(entry_price)
        
        if contract_size is None or contract_size <= 0:
            logging.error(f"❌ {symbol} - {side.capitalize()}: 契約サイズが不正なため、取引をスキップします。")
            trade_result = {'status': 'error', 'error_message': '契約サイズ計算失敗'}
        else:
            # 2. 取引の実行 (エントリー + SL/TP)
            trade_result = await execute_trade(signal, contract_size)
    
    # 3. 取引結果の通知とログ記録
    if trade_result['status'] == 'ok' or TEST_MODE:
        # 成功の場合、最終的なポジション情報 (entry_price, filled_usdt) を signal にマージし、ポジションリストに追加
        if not TEST_MODE:
            # 実際の約定情報で signal を更新
            signal['entry_price'] = trade_result['filled_price']
            signal['filled_usdt'] = trade_result['filled_usdt']
            signal['contracts'] = trade_result['filled_amount']
            
            # 新しいポジションを作成し、グローバルリストに追加
            new_position = {
                'symbol': symbol,
                'side': side,
                'contracts': trade_result['filled_amount'],
                'entry_price': trade_result['filled_price'],
                'filled_usdt': trade_result['filled_usdt'],
                'liquidation_price': calculate_liquidation_price(trade_result['filled_price'], LEVERAGE, side),
                'stop_loss': signal['stop_loss'],
                'take_profit': signal['take_profit'],
                'timestamp': int(time.time() * 1000),
                'leverage': LEVERAGE,
            }
            OPEN_POSITIONS.append(new_position)
            
        # 最終的な通知
        notification_message = format_telegram_message(signal, "取引シグナル", current_threshold, trade_result)
        await send_telegram_notification(notification_message)
        
        # ログ記録
        log_signal(signal, "Entry Signal Executed", trade_result)
        
        # 4. クールダウン時間の更新
        LAST_SIGNAL_TIME[symbol] = time.time()
        
    else:
        # 失敗の場合も通知 (ただし、クールダウンはしない)
        notification_message = format_telegram_message(signal, "取引シグナル", current_threshold, trade_result)
        await send_telegram_notification(notification_message)
        log_signal(signal, "Entry Signal Failed", trade_result)


# ====================================================================================
# POSITION MONITORING
# ====================================================================================

async def check_and_manage_open_positions():
    """
    オープンポジションを監視し、SL/TPに達したか、または清算価格に近づいていないかをチェックする。
    """
    global OPEN_POSITIONS, EXCHANGE_CLIENT, IS_CLIENT_READY
    
    if not OPEN_POSITIONS:
        return
        
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.error("❌ ポジション監視失敗: CCXTクライアントが準備できていません。")
        return

    symbols_to_monitor = [p['symbol'] for p in OPEN_POSITIONS]
    
    # 全ての監視銘柄の最新価格を一度に取得
    try:
        # MEXCはfetch_tickersで最新価格を取得
        tickers = await EXCHANGE_CLIENT.fetch_tickers(symbols=symbols_to_monitor)
    except Exception as e:
        logging.error(f"❌ ポジション監視価格取得失敗: {e}")
        # 価格が取得できない場合は、今回のチェックはスキップ
        return
        
    positions_to_close = []
    new_open_positions = []
    
    for position in OPEN_POSITIONS:
        symbol = position['symbol']
        side = position['side']
        sl = position['stop_loss']
        tp = position['take_profit']
        entry_price = position['entry_price']
        
        # 最新価格の取得
        ticker = tickers.get(symbol)
        if not ticker or 'last' not in ticker:
            new_open_positions.append(position) # 価格が取れない場合は次回に持ち越し
            continue
            
        current_price = ticker['last']
        
        trigger_type = None
        
        # ロングポジションのチェック
        if side == 'long':
            if current_price <= sl:
                trigger_type = 'Stop_Loss'
            elif current_price >= tp:
                trigger_type = 'Take_Profit'
                
        # ショートポジションのチェック
        elif side == 'short':
            if current_price >= sl:
                trigger_type = 'Stop_Loss'
            elif current_price <= tp:
                trigger_type = 'Take_Profit'

        if trigger_type:
            # SL/TPがトリガーされた
            positions_to_close.append({
                'position': position,
                'exit_price': current_price,
                'exit_type': trigger_type
            })
        else:
            # ポジションを維持
            new_open_positions.append(position)
            
    # グローバルポジションリストを更新 (クローズされたものを除く)
    OPEN_POSITIONS = new_open_positions
    
    # 決済が必要なポジションを処理
    for item in positions_to_close:
        await close_position_and_notify(item['position'], item['exit_price'], item['exit_type'])


async def close_position_and_notify(position: Dict, exit_price: float, exit_type: str) -> None:
    """
    ポジションを強制的に決済し、結果を通知する。
    """
    global EXCHANGE_CLIENT
    
    symbol = position['symbol']
    side = position['side']
    contracts = position['contracts']
    entry_price = position['entry_price']
    
    # 決済サイドはエントリーの逆
    close_side = 'sell' if side == 'long' else 'buy'
    
    # MEXCの場合、シンボルを変換
    exchange_symbol = EXCHANGE_CLIENT.markets[symbol]['symbol'] if symbol in EXCHANGE_CLIENT.markets else symbol
    
    pnl_rate = 0.0
    pnl_usdt = 0.0
    
    # PnLの計算
    if entry_price > 0 and exit_price > 0:
        if side == 'long':
            pnl_rate = (exit_price - entry_price) / entry_price
        else: # 'short'
            pnl_rate = (entry_price - exit_price) / entry_price
            
        # PnL (USDT) = 名目価値 * PnL率 * レバレッジ
        pnl_usdt = abs(position['filled_usdt'] * pnl_rate * LEVERAGE)
        # PnLの符号を修正 (pnl_rateが既に符号を持っているため、absを外す)
        pnl_usdt = position['filled_usdt'] * pnl_rate * LEVERAGE 
    
    trade_result = {
        'status': 'ok',
        'entry_price': entry_price,
        'exit_price': exit_price,
        'exit_type': exit_type,
        'pnl_usdt': pnl_usdt,
        'pnl_rate': pnl_rate,
        'filled_amount': abs(contracts),
        'error_message': None
    }
    
    try:
        # 決済注文 (成行注文で全量をクローズ)
        close_order = await EXCHANGE_CLIENT.create_market_order(
            exchange_symbol, 
            close_side, 
            abs(contracts) # ポジションの絶対量
        )
        
        # 注文が約定するまで待機
        if close_order.get('status') != 'closed':
            await asyncio.sleep(2) 
            close_order = await EXCHANGE_CLIENT.fetch_order(close_order['id'], exchange_symbol)
            
        if close_order.get('status') not in ['closed', 'filled']:
            raise Exception(f"決済注文が約定しませんでした。ステータス: {close_order.get('status')}")

        logging.info(f"✅ {symbol} - {side.capitalize()}: {exit_type} トリガーにより決済完了。PNL: {pnl_usdt:+.2f} USDT ({pnl_rate*100:.2f}%)")
        
        trade_result['close_order_id'] = close_order['id']
        # 最終決済価格が close_order の average price になる可能性もあるが、ここではトリガー時の価格を使用

    except Exception as e:
        error_message = f"決済注文失敗: {e}"
        logging.error(f"❌ {symbol} - {side.capitalize()}: {error_message}", exc_info=True)
        trade_result['error_message'] = error_message
        trade_result['status'] = 'error'

    # 通知とログ記録
    current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
    notification_message = format_telegram_message(position, "ポジション決済", current_threshold, trade_result, exit_type)
    await send_telegram_notification(notification_message)
    log_signal(position, f"Position Closed - {exit_type}", trade_result)


# ====================================================================================
# SCHEDULERS & MAIN LOOP
# ====================================================================================

async def main_bot_scheduler():
    """
    BOTのメイン分析・取引スケジューラ。定期的に実行される。
    """
    global CURRENT_MONITOR_SYMBOLS, IS_FIRST_MAIN_LOOP_COMPLETED, GLOBAL_MACRO_CONTEXT, LAST_WEBSHARE_UPLOAD_TIME, WEBSHARE_UPLOAD_INTERVAL, LOOP_INTERVAL
    
    # 1. CCXTクライアントの初期化 (成功するまでリトライ)
    while not await initialize_exchange_client():
        logging.critical("🚨 CCXTクライアント初期化に失敗しました。10秒後にリトライします。")
        await asyncio.sleep(10)
        
    # 2. 初期セットアップ完了通知 (最初のメインループ開始前)
    account_status = await fetch_account_status()
    GLOBAL_MACRO_CONTEXT = await fetch_macro_context()
    current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
    
    startup_message = format_startup_message(account_status, GLOBAL_MACRO_CONTEXT, len(CURRENT_MONITOR_SYMBOLS), current_threshold)
    await send_telegram_notification(startup_message)
    
    logging.info("🚀 メインBOTスケジューラを開始します...")

    while True:
        try:
            start_time = time.time()
            
            # 3. 監視銘柄リストの動的更新 (低頻度)
            CURRENT_MONITOR_SYMBOLS = await fetch_top_volume_symbols()
            
            # 4. マクロ環境データの更新
            GLOBAL_MACRO_CONTEXT = await fetch_macro_context()
            current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
            
            # 5. 口座ステータスと既存ポジションの更新 (取引実行前)
            account_status = await fetch_account_status()
            if account_status.get('error'):
                 # 致命的なエラーがある場合、取引をスキップ
                 logging.error("❌ 口座ステータスエラーのため、今回の分析/取引をスキップします。")
                 await asyncio.sleep(LOOP_INTERVAL)
                 continue 
                 
            # 6. 最良の取引シグナルの探索
            best_signals = await find_best_signals(GLOBAL_MACRO_CONTEXT)
            
            # 7. シグナルの処理 (取引実行)
            for signal in best_signals:
                await process_entry_signal(signal, GLOBAL_MACRO_CONTEXT)
                # 複数の取引を同時に実行する際、APIレートリミットを考慮して遅延を挿入
                await asyncio.sleep(1.0)
                
            # 8. 定時レポート (取引閾値未満の最高スコア)
            await notify_highest_analysis_score()
            
            # 9. WebShareデータ送信 (低頻度)
            if time.time() - LAST_WEBSHARE_UPLOAD_TIME > WEBSHARE_UPLOAD_INTERVAL:
                webshare_data = {
                    'status': 'ok',
                    'timestamp': datetime.now(JST).isoformat(),
                    'account': account_status,
                    'macro_context': GLOBAL_MACRO_CONTEXT,
                    'open_positions': OPEN_POSITIONS,
                    'last_analysis_signals': LAST_ANALYSIS_SIGNALS[:5], # 上位5件
                    'bot_version': BOT_VERSION,
                }
                await send_webshare_update(webshare_data)
                LAST_WEBSHARE_UPLOAD_TIME = time.time()
                
            # 10. ループの完了
            end_time = time.time()
            elapsed = end_time - start_time
            sleep_duration = max(0, LOOP_INTERVAL - elapsed)
            
            IS_FIRST_MAIN_LOOP_COMPLETED = True
            logging.info(f"✅ メインBOTループ完了。所要時間: {elapsed:.2f}秒。次回実行まで {sleep_duration:.0f}秒待機。")

            await asyncio.sleep(sleep_duration)
            
        except Exception as e:
            # メインBOTループでの予期せぬエラーは重大
            logging.error(f"❌ メインBOTループでエラーが発生しました: {e}", exc_info=True)
            await send_telegram_notification(f"🚨 <b>メインBOTループエラー</b>\n致命的なエラーが発生しました: <code>{e}</code>。再試行します。")
            
            # クライアント接続が切れた可能性もあるため、再初期化を試みる
            if not IS_CLIENT_READY:
                 await initialize_exchange_client()
                 
            # エラー発生時も強制的に次のループへ
            await asyncio.sleep(LOOP_INTERVAL)


async def position_monitor_scheduler():
    """
    ポジションのSL/TPトリガーを監視し、決済を実行するスケジューラ。
    メインループとは独立して高頻度で実行される。
    """
    # メインループが一度完了してから監視を開始
    while not IS_FIRST_MAIN_LOOP_COMPLETED:
        await asyncio.sleep(1)
        
    logging.info("👁️ ポジション監視スケジューラを開始します...")
    
    while True:
        try:
            # ポジションのSL/TPチェックと決済
            await check_and_manage_open_positions()
            
            # 次のチェックまで待機
            await asyncio.sleep(MONITOR_INTERVAL)
            
        except Exception as e:
            logging.error(f"❌ ポジション監視スケジューラでエラーが発生しました: {e}", exc_info=True)
            # エラーが発生しても監視は続ける
            await asyncio.sleep(MONITOR_INTERVAL)


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
        logging.error(f"🚨 捕捉されなかったエラーが発生しました: {exc}", exc_info=True)
    
    return JSONResponse(
        status_code=500,
        content={"message": f"Internal Server Error: {exc}"},
    )


# ====================================================================================
# MAIN ENTRY POINT
# ====================================================================================

if __name__ == "__main__":
    
    # Render環境で実行されることを想定し、uvicornを直接呼び出す
    # ホスト '0.0.0.0' は外部からのアクセスを許可するために必要
    # 環境変数 PORT が設定されている場合はそのポートを使用
    port = int(os.getenv("PORT", 8000))
    
    logging.info(f"Uvicornを起動します。ポート: {port}")
    # 標準出力にロギングしないように disable_lifespan=True を設定 (Renderのログ出力に対応)
    uvicorn.run("main_render:app", host="0.0.0.0", port=port, log_level="info", reload=False, limit_concurrency=10, workers=1)
