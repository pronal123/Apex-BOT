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
    logging.warning("⚠️ WARNING: TEST_MODE is active. Trading is disabled.")

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
                    'raw_info': p.get('info', {}),
                    'id': str(uuid.uuid4()) # ユニークIDを付与
                })
        
        # グローバル変数に保存
        global OPEN_POSITIONS
        OPEN_POSITIONS = open_positions
        logging.info(f"📊 現在オープン中のポジション: {len(OPEN_POSITIONS)} 銘柄を検出しました。")
        
        return {
            'total_usdt_balance': ACCOUNT_EQUITY_USDT, 
            'open_positions': open_positions, 
            'error': False
        }
    except ccxt.DDoSProtection as e:
        logging.error(f"❌ DDoS保護エラー: レートリミットに達した可能性があります。{e}")
    except ccxt.ExchangeError as e:
        logging.error(f"❌ 取引所エラー (口座ステータス): {e}")
    except Exception as e:
        logging.error(f"❌ 口座ステータス取得中の予期せぬエラー: {e}", exc_info=True)

    return {'total_usdt_balance': 0.0, 'open_positions': [], 'error': True}

async def fetch_ohlcv_with_retry(symbol: str, timeframe: str, limit: int = 500) -> Optional[pd.DataFrame]:
    """OHLCVデータを取得し、失敗した場合はリトライする。"""
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.error("❌ OHLCV取得失敗: CCXTクライアントが準備できていません。")
        return None
        
    max_retries = 3
    for attempt in range(max_retries):
        try:
            # fetch_ohlcvを非同期で実行
            ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(symbol, timeframe, limit=limit)
            
            if not ohlcv:
                logging.warning(f"⚠️ {symbol} - {timeframe}: OHLCVデータが空でした。")
                return None

            # Pandas DataFrameに変換
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms').dt.tz_localize(timezone.utc).dt.tz_convert(JST)
            df.set_index('timestamp', inplace=True)
            
            return df
            
        except ccxt.RequestTimeout as e:
            logging.warning(f"⚠️ {symbol} - {timeframe}: リクエストタイムアウト (試行 {attempt + 1}/{max_retries}): {e}")
        except ccxt.NetworkError as e:
            logging.warning(f"⚠️ {symbol} - {timeframe}: ネットワークエラー (試行 {attempt + 1}/{max_retries}): {e}")
        except ccxt.DDoSProtection as e:
             logging.warning(f"⚠️ {symbol} - {timeframe}: DDoS保護/レートリミット (試行 {attempt + 1}/{max_retries}): {e}")
        except Exception as e:
            logging.error(f"❌ {symbol} - {timeframe}: OHLCV取得中の予期せぬエラー (試行 {attempt + 1}/{max_retries}): {e}")
            
        # リトライ前に待機
        await asyncio.sleep(2 ** attempt) # 指数バックオフ
        
    logging.error(f"❌ {symbol} - {timeframe}: OHLCVデータの取得に失敗しました。")
    return None

async def fetch_top_volume_symbols() -> List[str]:
    """
    取引所の全てのUSDTペアから、過去24時間の出来高上位TOP_SYMBOL_LIMITの銘柄を取得する。
    失敗した場合はDEFAULT_SYMBOLSを返す。
    """
    global EXCHANGE_CLIENT, SKIP_MARKET_UPDATE, DEFAULT_SYMBOLS
    
    if SKIP_MARKET_UPDATE:
        logging.info("ℹ️ SKIP_MARKET_UPDATEがTrueのため、出来高ランキングの更新をスキップします。")
        return DEFAULT_SYMBOLS
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.error("❌ 出来高シンボル取得失敗: CCXTクライアントが準備できていません。DEFAULT_SYMBOLSを返します。")
        return DEFAULT_SYMBOLS

    try:
        # fetch_tickersを非同期で実行
        tickers = await EXCHANGE_CLIENT.fetch_tickers()
        
        if not tickers:
            raise Exception("fetch_tickers returned empty data.")
        
        # 出来高 (quote volume) があり、USDT建ての先物/スワップペアのみをフィルタリング
        usdt_future_tickers = []
        for symbol, ticker in tickers.items():
            # ccxtのシンボル名が '/USDT' で終わり、かつ出来高 (quoteVolume) が存在し、
            # かつ ccxt.markets で先物/スワップと判定されるもの
            market = EXCHANGE_CLIENT.markets.get(symbol)
            if ticker and \
               '/USDT' in symbol and \
               market and \
               market.get('type') in ['swap', 'future'] and \
               market.get('active', False) and \
               ticker.get('quoteVolume') is not None:
                
                try:
                    quote_volume = float(ticker['quoteVolume'])
                    usdt_future_tickers.append({
                        'symbol': symbol,
                        'quoteVolume': quote_volume
                    })
                except (ValueError, TypeError):
                    continue

        if not usdt_future_tickers:
            logging.warning("⚠️ 出来高データを持つUSDT建て先物ペアが見つかりませんでした。DEFAULT_SYMBOLSを返します。")
            return DEFAULT_SYMBOLS
            
        # 出来高で降順ソート
        sorted_tickers = sorted(usdt_future_tickers, key=lambda x: x['quoteVolume'], reverse=True)
        
        # TOP Nを取得
        top_symbols = [t['symbol'] for t in sorted_tickers[:TOP_SYMBOL_LIMIT]]
        
        # DEFAULT_SYMBOLSに設定されているが、TOP Nに含まれなかった主要な銘柄を追加で含める
        # BTC/USDT, ETH/USDT, SOL/USDT は最低限含めるべき
        must_include = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
        for s in must_include:
            if s not in top_symbols and s in DEFAULT_SYMBOLS:
                 top_symbols.append(s)

        logging.info(f"✅ 出来高ランキング更新成功。{len(top_symbols)} 銘柄を監視対象とします。")
        
        return top_symbols

    except ccxt.ExchangeError as e:
        # 💡 致命的エラー修正強化: fetch_tickersのAttributeError対策
        logging.error(f"❌ 取引所エラー (fetch_tickers): {e}")
    except Exception as e:
        logging.error(f"❌ 出来高シンボル取得中の予期せぬエラー: {e}", exc_info=True)
        
    logging.warning("⚠️ 出来高ランキングの取得に失敗したため、デフォルトの監視銘柄リストを使用します。")
    return DEFAULT_SYMBOLS

async def fetch_fear_and_greed_index() -> Dict:
    """
    Fear & Greed Index (FGI) を取得し、スコア計算用のプロキシ値を生成する。
    値の範囲: 0 (Extreme Fear) - 100 (Extreme Greed)
    プロキシ値の範囲: -0.05 (Extreme Fear) - +0.05 (Extreme Greed)
    """
    global FGI_API_URL, GLOBAL_MACRO_CONTEXT, FGI_PROXY_BONUS_MAX
    
    try:
        response = await asyncio.to_thread(requests.get, FGI_API_URL, timeout=5)
        response.raise_for_status()
        
        data = response.json().get('data')
        if not data:
            raise Exception("FGI API returned no data.")
            
        fgi_value = int(data[0]['value'])
        
        # FGI (0-100) を -1 から +1 の範囲に正規化
        # -1 (0: Extreme Fear) -> +1 (100: Extreme Greed)
        normalized_fgi = (fgi_value - 50) / 50 
        
        # 正規化された値を、最大ボーナス値の範囲 (-FGI_PROXY_BONUS_MAX から +FGI_PROXY_BONUS_MAX) にスケーリング
        fgi_proxy = normalized_fgi * FGI_PROXY_BONUS_MAX
        
        result = {
            'fgi_raw_value': fgi_value,
            'fgi_proxy': fgi_proxy,
            'fgi_classification': data[0]['value_classification']
        }
        
        logging.info(f"🌍 FGI取得成功: {fgi_value} ({data[0]['value_classification']}), Proxy: {fgi_proxy:.4f}")
        return result
        
    except requests.exceptions.RequestException as e:
        logging.error(f"❌ FGI取得リクエストエラー: {e}")
    except Exception as e:
        logging.error(f"❌ FGI取得中の予期せぬエラー: {e}", exc_info=True)
        
    logging.warning("⚠️ FGI取得に失敗しました。デフォルト値を使用します。")
    return {'fgi_proxy': 0.0, 'fgi_raw_value': 'N/A', 'fgi_classification': 'N/A'}


# ====================================================================================
# CORE TRADING LOGIC - TECHNICAL ANALYSIS & SCORING
# ====================================================================================

def calculate_technical_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    データフレームにテクニカル指標を追加する。
    """
    # 💡 SMA 200 (長期トレンドの基準)
    df['SMA_200'] = ta.sma(df['close'], length=LONG_TERM_SMA_LENGTH)
    
    # 💡 ATR (ボラティリティ/リスク管理の基準)
    df['ATR'] = ta.atr(df['high'], df['low'], df['close'], length=ATR_LENGTH)
    
    # 💡 RSI (モメンタム)
    df['RSI'] = ta.rsi(df['close'], length=14)
    df['RSI_DIVERGENCE'] = 0 # 後でロジックを実装する
    
    # 💡 MACD (短期モメンタムとクロス)
    macd_df = ta.macd(df['close'], fast=12, slow=26, signal=9)
    df = pd.concat([df, macd_df], axis=1) # MACD_12_26_9, MACDh_12_26_9, MACDs_12_26_9
    
    # 💡 Bollinger Bands (ボラティリティと価格の極端さ)
    bb_df = ta.bbands(df['close'], length=20, std=2)
    df = pd.concat([df, bb_df], axis=1) # BBL_20_2.0, BBMW_20_2.0, BBP_20_2.0, BBU_20_2.0, BBB_20_2.0
    
    # 💡 OBV (出来高モメンタム)
    df['OBV'] = ta.obv(df['close'], df['volume'])
    df['OBV_SMA'] = ta.sma(df['OBV'], length=20)

    # 💡 ADX (トレンドの強さ)
    df['ADX'] = ta.adx(df['high'], df['low'], df['close'], length=14)['ADX_14']
    
    return df.dropna()


def analyze_trend_and_momentum(df: pd.DataFrame, current_price: float, side: str) -> Dict[str, Any]:
    """
    トレンド、モメンタム、ボラティリティの要因を分析し、スコアへの貢献度を計算する。
    """
    
    # 最新のインジケータ値を取得
    last = df.iloc[-1]
    
    # --------------------------------------------------
    # 1. 長期トレンド (SMA 200) - 順張り/逆張りペナルティ
    # --------------------------------------------------
    trend_penalty_value = 0.0
    if not np.isnan(last['SMA_200']):
        if side == 'long':
            # 順張り: 価格がSMA 200の上にある = ボーナス
            if current_price > last['SMA_200']:
                trend_penalty_value = LONG_TERM_REVERSAL_PENALTY
            # 逆張り: 価格がSMA 200の下にある = ペナルティ
            elif current_price < last['SMA_200']:
                trend_penalty_value = -LONG_TERM_REVERSAL_PENALTY / 2.0 # 逆張りでもペナルティを半分に抑える
            
        elif side == 'short':
            # 順張り: 価格がSMA 200の下にある = ボーナス
            if current_price < last['SMA_200']:
                trend_penalty_value = LONG_TERM_REVERSAL_PENALTY
            # 逆張り: 価格がSMA 200の上にある = ペナルティ
            elif current_price > last['SMA_200']:
                trend_penalty_value = -LONG_TERM_REVERSAL_PENALTY / 2.0 
    
    
    # --------------------------------------------------
    # 2. MACD モメンタム (短期方向一致) - MACDhとシグナルのクロス
    # --------------------------------------------------
    macd_penalty_value = 0.0
    # MACDh (MACD Histogram) を使用して勢いを評価
    # MACDh > 0 and 増加傾向 (MACDhが直前よりも大きい) -> 強い勢い
    # MACDh < 0 and 減少傾向 (MACDhが直前よりも小さい) -> 強い勢い
    
    if len(df) >= 2:
        prev_last = df.iloc[-2]
        macd_hist_name = 'MACDh_12_26_9'
        
        if not np.isnan(last[macd_hist_name]):
            
            # モメンタムが加速しているか
            is_increasing_momentum = last[macd_hist_name] > prev_last[macd_hist_name]
            
            if side == 'long':
                # ロングシグナル: MACDhが0以上で、増加傾向にある (モメンタム一致)
                if last[macd_hist_name] > 0 and is_increasing_momentum:
                    macd_penalty_value = MACD_CROSS_PENALTY 
                # ロングシグナル: MACDhがマイナス域だが、増加に転じている（トレンド転換期）
                # elif last[macd_hist_name] < 0 and is_increasing_momentum:
                #     macd_penalty_value = MACD_CROSS_PENALTY / 2.0
                # 不一致: MACDhが減少している (モメンタム失速/逆行)
                elif not is_increasing_momentum:
                    macd_penalty_value = -MACD_CROSS_PENALTY
                    
            elif side == 'short':
                # ショートシグナル: MACDhが0未満で、減少傾向にある (モメンタム一致)
                if last[macd_hist_name] < 0 and not is_increasing_momentum:
                    macd_penalty_value = MACD_CROSS_PENALTY
                # ショートシグナル: MACDhがプラス域だが、減少に転じている
                # elif last[macd_hist_name] > 0 and not is_increasing_momentum:
                #     macd_penalty_value = MACD_CROSS_PENALTY / 2.0
                # 不一致: MACDhが増加している (モメンタム失速/逆行)
                elif is_increasing_momentum:
                    macd_penalty_value = -MACD_CROSS_PENALTY

    # --------------------------------------------------
    # 3. RSI モメンタム加速/適正水準 (RSI 40-60)
    # --------------------------------------------------
    rsi_momentum_bonus_value = 0.0
    if not np.isnan(last['RSI']):
        
        # Long: RSIが売られすぎ水準から回復し、適正水準 (40-60) 以上である
        if side == 'long':
            # 適正なモメンタム加速 (40以上)
            if last['RSI'] >= RSI_MOMENTUM_LOW: 
                rsi_momentum_bonus_value = STRUCTURAL_PIVOT_BONUS # STRUCTURAL_PIVOT_BONUS を流用
            # 強すぎるモメンタム (70以上) はペナルティ
            elif last['RSI'] > 70:
                rsi_momentum_bonus_value = -STRUCTURAL_PIVOT_BONUS 
                
        # Short: RSIが買われすぎ水準から回復し、適正水準 (40-60) 以下である
        elif side == 'short':
            # 適正なモメンタム加速 (60以下)
            if last['RSI'] <= (100 - RSI_MOMENTUM_LOW):
                rsi_momentum_bonus_value = STRUCTURAL_PIVOT_BONUS
            # 弱すぎるモメンタム (30以下) はペナルティ
            elif last['RSI'] < 30:
                rsi_momentum_bonus_value = -STRUCTURAL_PIVOT_BONUS

    # --------------------------------------------------
    # 4. OBV 出来高モメンタム確証
    # --------------------------------------------------
    obv_momentum_bonus_value = 0.0
    if len(df) >= 2 and not np.isnan(last['OBV']) and not np.isnan(last['OBV_SMA']):
        
        is_obv_above_sma = last['OBV'] > last['OBV_SMA']
        prev_obv_above_sma = df.iloc[-2]['OBV'] > df.iloc[-2]['OBV_SMA']
        
        # Long: OBVがSMAを上回っており、出来高が価格上昇を確証
        if side == 'long':
            if is_obv_above_sma and (last['OBV'] > df.iloc[-2]['OBV']): # 上回っており、かつOBVも上昇している
                obv_momentum_bonus_value = OBV_MOMENTUM_BONUS
        
        # Short: OBVがSMAを下回っており、出来高が価格下落を確証
        elif side == 'short':
            if not is_obv_above_sma and (last['OBV'] < df.iloc[-2]['OBV']): # 下回っており、かつOBVも下降している
                obv_momentum_bonus_value = OBV_MOMENTUM_BONUS

    # --------------------------------------------------
    # 5. ボラティリティ過熱ペナルティ (BBの幅が広すぎる場合)
    # --------------------------------------------------
    volatility_penalty_value = 0.0
    bb_width_name = 'BBB_20_2.0' # Bollinger Bands Bandwidth
    
    if not np.isnan(last[bb_width_name]):
        # BB幅 (価格に対する割合%) が閾値を超えている場合
        bb_width_percent = last[bb_width_name] / 100.0 # BBBはパーセント表示 (例: 5.0 -> 5%)
        
        if bb_width_percent > VOLATILITY_BB_PENALTY_THRESHOLD:
            # 閾値からの超過分に応じてペナルティを線形に増加させる
            excess_factor = (bb_width_percent - VOLATILITY_BB_PENALTY_THRESHOLD) / VOLATILITY_BB_PENALTY_THRESHOLD
            max_penalty = 0.05 # 最大ペナルティ
            volatility_penalty_value = -min(max_penalty, excess_factor * 0.02) # 最大0.05まで
        
    
    # --------------------------------------------------
    # 6. RSI ダイバージェンス (リバーサルシグナル) - 簡易版
    # --------------------------------------------------
    # 簡易版: 過去10期間の高値/安値とRSIの傾向が逆転しているか
    divergence_bonus = 0.0
    if len(df) >= 10:
        last_10 = df.iloc[-10:]
        
        if side == 'long':
            # Bullish Divergence: Price Low is Lower (安値切り下げ) but RSI Low is Higher (RSI安値切り上げ)
            price_low_is_lower = current_price < last_10['low'].min()
            rsi_low_is_higher = last['RSI'] > last_10['RSI'].min()
            
            if price_low_is_lower and rsi_low_is_higher and last['RSI'] < RSI_MOMENTUM_LOW:
                divergence_bonus = RSI_DIVERGENCE_BONUS
                
        elif side == 'short':
            # Bearish Divergence: Price High is Higher (高値切り上げ) but RSI High is Lower (RSI高値切り下げ)
            price_high_is_higher = current_price > last_10['high'].max()
            rsi_high_is_lower = last['RSI'] < last_10['RSI'].max()
            
            if price_high_is_higher and rsi_high_is_lower and last['RSI'] > (100 - RSI_MOMENTUM_LOW):
                divergence_bonus = RSI_DIVERGENCE_BONUS


    return {
        'long_term_reversal_penalty_value': trend_penalty_value,
        'macd_penalty_value': macd_penalty_value,
        'rsi_momentum_bonus_value': rsi_momentum_bonus_value,
        'volatility_penalty_value': volatility_penalty_value,
        'obv_momentum_bonus_value': obv_momentum_bonus_value,
        'rsi_divergence_bonus_value': divergence_bonus,
        # インジケータの生値
        'raw_rsi': float(last['RSI']),
        'raw_macd_hist': float(last[macd_hist_name]) if not np.isnan(last[macd_hist_name]) else 0.0,
        'raw_sma_200': float(last['SMA_200']) if not np.isnan(last['SMA_200']) else 0.0,
        'raw_adx': float(last['ADX']) if not np.isnan(last['ADX']) else 0.0,
    }


def calculate_trade_score(symbol: str, timeframe: str, df: pd.DataFrame, side: str, macro_context: Dict) -> Optional[Dict[str, Any]]:
    """
    テクニカル分析の結果を統合し、最終的な取引スコアを計算する。
    """
    if df.empty:
        return None
        
    current_price = df['close'].iloc[-1]
    
    # テクニカル分析の実行
    tech_data = analyze_trend_and_momentum(df, current_price, side)
    
    # --------------------------------------------------
    # 1. ベーススコア
    # --------------------------------------------------
    score = BASE_SCORE # 0.40

    # --------------------------------------------------
    # 2. テクニカルボーナス/ペナルティ
    # --------------------------------------------------
    score += tech_data['long_term_reversal_penalty_value']
    score += tech_data['macd_penalty_value']
    score += tech_data['rsi_momentum_bonus_value']
    score += tech_data['volatility_penalty_value']
    score += tech_data['obv_momentum_bonus_value']
    score += tech_data['rsi_divergence_bonus_value']
    
    # 構造的ボーナス (最低限の優位性を示す)
    tech_data['structural_pivot_bonus'] = STRUCTURAL_PIVOT_BONUS
    score += STRUCTURAL_PIVOT_BONUS
    
    # --------------------------------------------------
    # 3. 流動性ボーナス (トップ銘柄補正)
    # --------------------------------------------------
    liquidity_bonus_value = 0.0
    if symbol in CURRENT_MONITOR_SYMBOLS[:10]: # トップ10銘柄に大きなボーナス
        liquidity_bonus_value = LIQUIDITY_BONUS_MAX
    elif symbol in CURRENT_MONITOR_SYMBOLS[10:TOP_SYMBOL_LIMIT]: # 次の層に小さなボーナス
        liquidity_bonus_value = LIQUIDITY_BONUS_MAX / 2.0 
        
    tech_data['liquidity_bonus_value'] = liquidity_bonus_value
    score += liquidity_bonus_value
    
    # --------------------------------------------------
    # 4. マクロ環境ボーナス/ペナルティ (FGI)
    # --------------------------------------------------
    fgi_proxy = macro_context.get('fgi_proxy', 0.0)
    forex_bonus = macro_context.get('forex_bonus', 0.0)
    
    # ロングシグナル: FGIがプラス (Greed) ならボーナス、マイナス (Fear) ならペナルティ
    if side == 'long':
        sentiment_fgi_proxy_bonus = fgi_proxy
    # ショートシグナル: FGIがマイナス (Fear) ならボーナス、プラス (Greed) ならペナルティ
    elif side == 'short':
        sentiment_fgi_proxy_bonus = -fgi_proxy
        
    # Forexボーナスを統合 (現在0.0)
    sentiment_fgi_proxy_bonus += forex_bonus
    
    # マクロ影響が最大ボーナス値を超えないようにクリッピング
    sentiment_fgi_proxy_bonus = max(-FGI_PROXY_BONUS_MAX, min(FGI_PROXY_BONUS_MAX, sentiment_fgi_proxy_bonus))
    
    tech_data['sentiment_fgi_proxy_bonus'] = sentiment_fgi_proxy_bonus
    score += sentiment_fgi_proxy_bonus

    # 最終的なスコアを0.0から1.0の範囲にクリッピング
    final_score = max(0.0, min(1.0, score))

    # ATRに基づく動的リスク管理
    last_atr = df['ATR'].iloc[-1] if not df['ATR'].empty and not np.isnan(df['ATR'].iloc[-1]) else None
    
    if last_atr is None or last_atr <= 0:
        logging.warning(f"⚠️ {symbol} - {timeframe}: ATR値が無効です。シグナルをスキップします。")
        return None

    # SL幅 = ATR * 乗数
    sl_distance = last_atr * ATR_MULTIPLIER_SL
    
    # 最低リスク幅 (MIN_RISK_PERCENT) を考慮
    min_risk_distance = current_price * MIN_RISK_PERCENT
    sl_distance = max(sl_distance, min_risk_distance)
    
    # TP幅 = SL幅 * RR_RATIO_TARGET (最小リスクリワード比率)
    tp_distance = sl_distance * RR_RATIO_TARGET

    # SL/TP価格の計算
    if side == 'long':
        stop_loss = current_price - sl_distance
        take_profit = current_price + tp_distance
    else: # side == 'short'
        stop_loss = current_price + sl_distance
        take_profit = current_price - tp_distance
        
    # 清算価格の推定
    liquidation_price = calculate_liquidation_price(
        entry_price=current_price, 
        leverage=LEVERAGE, 
        side=side, 
        maintenance_margin_rate=MIN_MAINTENANCE_MARGIN_RATE
    )

    result = {
        'symbol': symbol,
        'timeframe': timeframe,
        'side': side,
        'score': final_score,
        'current_price': current_price,
        'entry_price': current_price, # 成行注文を想定
        'stop_loss': stop_loss,
        'take_profit': take_profit,
        'liquidation_price': liquidation_price,
        'risk_distance': sl_distance,
        'reward_distance': tp_distance,
        'rr_ratio': RR_RATIO_TARGET, # 設定RRR
        'tech_data': tech_data,
        'atr_value': last_atr,
        'generated_at': time.time()
    }
    
    return result


async def analyze_single_symbol(symbol: str, macro_context: Dict) -> List[Dict]:
    """
    指定されたシンボルと全てのターゲットタイムフレームで分析を実行する。
    """
    signals = []
    
    for tf in TARGET_TIMEFRAMES:
        limit = REQUIRED_OHLCV_LIMITS.get(tf, 500)
        df = await fetch_ohlcv_with_retry(symbol, tf, limit=limit)
        
        if df is None or df.shape[0] < LONG_TERM_SMA_LENGTH + ATR_LENGTH:
            logging.warning(f"⚠️ {symbol} - {tf}: 必要なデータ量が不足しています ({df.shape[0]}/{LONG_TERM_SMA_LENGTH + ATR_LENGTH})。スキップします。")
            continue

        current_price = df['close'].iloc[-1]
        
        # 💡 インジケータ計算
        df_indicators = calculate_technical_indicators(df)
        
        if df_indicators.empty:
            continue
            
        # ロング/ショート両方のスコアを計算
        for side in ['long', 'short']:
            score_data = calculate_trade_score(symbol, tf, df_indicators, side, macro_context)
            
            if score_data is None:
                continue

            # スコアが一定の閾値を超えた場合、またはデバッグ用に全てのスコアを収集
            if score_data['score'] >= BASE_SCORE:
                signals.append(score_data)
                
    return signals


async def find_best_signals(macro_context: Dict) -> List[Dict]:
    """
    監視対象の全シンボルで分析を実行し、最も高いスコアを持つシグナルを見つける。
    """
    global CURRENT_MONITOR_SYMBOLS, LAST_ANALYSIS_SIGNALS
    
    all_signals: List[Dict] = []
    
    # 非同期タスクのリストを作成
    analysis_tasks = [analyze_single_symbol(symbol, macro_context) for symbol in CURRENT_MONITOR_SYMBOLS]
    
    # 全ての分析タスクを並行して実行
    results = await asyncio.gather(*analysis_tasks)
    
    # 結果を統合
    for symbol_signals in results:
        all_signals.extend(symbol_signals)
        
    # 最新の全分析シグナルをグローバル変数に保存 (定時通知用)
    LAST_ANALYSIS_SIGNALS = all_signals

    # スコアの降順でソート
    sorted_signals = sorted(all_signals, key=lambda x: x.get('score', 0.0), reverse=True)
    
    if not sorted_signals:
        return []

    # 市場環境に応じた動的取引閾値を取得
    current_threshold = get_current_threshold(macro_context)
    
    # 閾値を超えたシグナルを抽出
    trade_ready_signals = [
        s for s in sorted_signals 
        if s['score'] >= current_threshold
    ]
    
    if not trade_ready_signals:
        logging.info(f"ℹ️ 最高のシグナルスコア {sorted_signals[0]['score']:.2f} が閾値 {current_threshold:.2f} に達しませんでした。")
        return []
        
    # 銘柄ごとのクールダウンを考慮
    final_best_signals = []
    
    for signal in trade_ready_signals:
        symbol = signal['symbol']
        current_time = time.time()
        last_trade_time = LAST_SIGNAL_TIME.get(symbol, 0.0)
        
        # クールダウン期間をチェック
        if current_time - last_trade_time < TRADE_SIGNAL_COOLDOWN:
            elapsed_hours = (current_time - last_trade_time) / 3600
            remaining_hours = (TRADE_SIGNAL_COOLDOWN / 3600) - elapsed_hours
            logging.info(f"ℹ️ {symbol} はクールダウン期間中です (前回取引から {elapsed_hours:.1f}h 経過 / 残り {remaining_hours:.1f}h)。スキップします。")
            continue
            
        final_best_signals.append(signal)

    if not final_best_signals:
        return []
        
    # クールダウンを考慮した後、スコア上位 N 件を返す
    return final_best_signals[:TOP_SIGNAL_COUNT]


# ====================================================================================
# CORE TRADING LOGIC - EXECUTION
# ====================================================================================

# 💡 致命的エラー修正強化: 注文失敗エラー (Amount can not be less than zero) 対策
async def execute_trade(signal: Dict) -> Dict:
    """
    シグナルに基づいて取引を実行し、SL/TP注文を設定する。
    """
    global EXCHANGE_CLIENT
    
    symbol = signal['symbol']
    side = signal['side']
    entry_price = signal['entry_price']
    stop_loss = signal['stop_loss']
    take_profit = signal['take_profit']
    
    if TEST_MODE:
        logging.warning(f"⚠️ TEST_MODE: {symbol} - {side.upper()} シグナルを検出しましたが、取引はスキップされます。")
        return {
            'status': 'test_mode', 
            'error_message': 'Test mode is active.',
            'filled_amount': FIXED_NOTIONAL_USDT / entry_price if entry_price > 0 else 0.0,
            'filled_usdt': FIXED_NOTIONAL_USDT,
            'entry_price': entry_price
        }

    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        return {'status': 'error', 'error_message': 'CCXT Client not ready.'}

    # 1. 注文数量の計算 (Notional Value based on FIXED_NOTIONAL_USDT)
    
    # 最小取引量を取得 (ccxtのmarket情報から)
    market = EXCHANGE_CLIENT.markets.get(symbol)
    if not market:
        return {'status': 'error', 'error_message': f'Market for {symbol} not found.'}

    # ロットサイズ (契約サイズ) をUSDT建ての名目ロットから計算
    if entry_price <= 0:
        return {'status': 'error', 'error_message': 'Entry price is invalid.'}

    base_amount = FIXED_NOTIONAL_USDT / entry_price
    
    # 契約サイズの丸め処理 (取引所の最小取引単位に合わせて調整)
    # amount_to_trade: 最終的に注文する契約サイズ
    amount_to_trade = EXCHANGE_CLIENT.amount_to_precision(symbol, base_amount)
    
    if float(amount_to_trade) <= 0:
         return {'status': 'error', 'error_message': f'Calculated amount {base_amount} is too small or precision error. Final amount: {amount_to_trade}.'}


    # 2. メインの成行注文の実行
    try:
        order_side = 'buy' if side == 'long' else 'sell'
        
        # 数量は絶対値で渡す必要がある
        absolute_amount = float(amount_to_trade)

        # 注文実行
        order = await EXCHANGE_CLIENT.create_market_order(
            symbol=symbol,
            side=order_side,
            amount=absolute_amount,
            params={'leverage': LEVERAGE} # レバレッジを再度確認のためにパラメーターに含める
        )

        # 注文の結果を待つ (ccxtはMarket Orderの場合、すぐにFilledになることが多い)
        # order.get('status') == 'closed' を確認
        if order.get('status') not in ['closed', 'filled']:
            logging.error(f"❌ {symbol} - {side.upper()}: 成行注文が完了しませんでした。Status: {order.get('status')}")
            # 注文が残っている場合はキャンセル処理を考慮すべきだが、ここでは割愛
            return {'status': 'error', 'error_message': f'Market order not filled. Status: {order.get("status")}'}
            
        filled_amount = float(order.get('filled', order.get('amount', 0.0)))
        filled_price = float(order.get('average', entry_price)) # 平均約定価格
        filled_usdt = filled_amount * filled_price
        
        # 3. SL/TP注文の実行
        # SL/TPはOCOまたはPost-Only Stop/Limit注文として処理する
        
        stop_order_id = None
        take_profit_order_id = None
        
        # ストップ注文 (STOP_MARKETを推奨)
        stop_side = 'sell' if side == 'long' else 'buy'
        stop_price = EXCHANGE_CLIENT.price_to_precision(symbol, stop_loss)
        
        try:
            # トリガー価格 (stopPrice) を使用したストップマーケット注文
            stop_order = await EXCHANGE_CLIENT.create_order(
                symbol=symbol,
                type='stop_market',
                side=stop_side,
                amount=filled_amount,
                params={
                    'stopPrice': stop_price,
                    'closeOnTrigger': True # ポジション全決済を意図 (取引所による)
                }
            )
            stop_order_id = stop_order.get('id')
            logging.info(f"✅ {symbol}: SL注文 (stop_market) を {stop_price} に設定しました。ID: {stop_order_id}")
        except Exception as e:
            logging.error(f"❌ {symbol}: SL注文の設定に失敗しました: {e}")
        
        # TP注文 (LIMITまたはTAKE_PROFIT_MARKETを推奨)
        take_profit_side = 'sell' if side == 'long' else 'buy'
        take_profit_price = EXCHANGE_CLIENT.price_to_precision(symbol, take_profit)
        
        try:
             # トリガー価格 (stopPrice) を使用したテイクプロフィットマーケット注文
            tp_order = await EXCHANGE_CLIENT.create_order(
                symbol=symbol,
                type='take_profit_market',
                side=take_profit_side,
                amount=filled_amount,
                params={
                    'stopPrice': take_profit_price,
                    'closeOnTrigger': True
                }
            )
            take_profit_order_id = tp_order.get('id')
            logging.info(f"✅ {symbol}: TP注文 (take_profit_market) を {take_profit_price} に設定しました。ID: {take_profit_order_id}")
        except Exception as e:
            logging.error(f"❌ {symbol}: TP注文の設定に失敗しました: {e}")
            
        
        # 成功時の結果
        return {
            'status': 'ok',
            'filled_amount': filled_amount,
            'filled_usdt': filled_usdt,
            'entry_price': filled_price,
            'stop_loss_id': stop_order_id,
            'take_profit_id': take_profit_order_id,
        }

    except ccxt.DDoSProtection as e:
        logging.error(f"❌ DDoS保護エラー: 注文失敗。{e}")
        return {'status': 'error', 'error_message': f'DDoS Protection: {e}'}
    except ccxt.InsufficientFunds as e:
        logging.error(f"❌ 証拠金不足エラー: 注文失敗。{e}")
        return {'status': 'error', 'error_message': f'Insufficient Funds: {e}'}
    except ccxt.InvalidOrder as e:
        logging.error(f"❌ 無効な注文エラー: 注文失敗。{e}")
        return {'status': 'error', 'error_message': f'Invalid Order: {e}'}
    except ccxt.ExchangeError as e:
        logging.error(f"❌ 取引所エラー: 注文失敗。{e}")
        return {'status': 'error', 'error_message': f'Exchange Error: {e}'}
    except Exception as e:
        logging.error(f"❌ 注文実行中の予期せぬエラー: {e}", exc_info=True)
        return {'status': 'error', 'error_message': f'Unexpected Error: {e}'}


async def check_and_manage_open_positions():
    """
    オープンポジションをチェックし、
    1. SL/TPが設定されていない場合は設定する
    2. 強制清算価格に近づいていないかチェックする (今回は清算価格通知のみ)
    """
    global OPEN_POSITIONS
    
    if not OPEN_POSITIONS:
        return
        
    positions_to_close: List[Dict] = []
    
    # 最新の価格を取得するために、ポジションのある銘柄のティッカーをまとめて取得
    symbols_to_fetch = [p['symbol'] for p in OPEN_POSITIONS]
    current_prices: Dict[str, float] = {}
    
    if EXCHANGE_CLIENT.has['fetchTickers']:
        try:
            tickers = await EXCHANGE_CLIENT.fetch_tickers(symbols_to_fetch)
            for symbol, ticker in tickers.items():
                if ticker and ticker.get('last') is not None:
                    current_prices[symbol] = ticker['last']
        except Exception as e:
            logging.error(f"❌ ポジション管理のためのティッカー取得に失敗: {e}")
            # エラーの場合は、処理を継続させるため、価格情報を空のままにする
            pass

    # 1. SL/TPチェックと設定、決済トリガーの確認
    for position in OPEN_POSITIONS[:]: # リストをイテレート中に変更するために[:]を使用
        symbol = position['symbol']
        side = position['side']
        entry_price = position['entry_price']
        
        current_price = current_prices.get(symbol)
        if current_price is None:
            # 価格が取得できない場合はスキップ
            logging.warning(f"⚠️ {symbol}: 最新の価格を取得できませんでした。SL/TPチェックをスキップします。")
            continue

        # 1-1. SL/TPが設定されていない場合、再計算/設定を試みる (主にBOT再起動時対応)
        if position['stop_loss'] == 0.0 or position['take_profit'] == 0.0:
            logging.warning(f"⚠️ {symbol} - {side.upper()}: SL/TP情報がポジションオブジェクトに不足しています。OHLCVを取得してSL/TPを再計算/設定を試みます。")
            
            # OHLCVを取得
            df_ohlcv = await fetch_ohlcv_with_retry(symbol, '1h', limit=500)
            if df_ohlcv is None:
                continue

            df_indicators = calculate_technical_indicators(df_ohlcv)
            if df_indicators.empty:
                continue

            # 💡 SL/TP計算を、スコア計算関数から流用して実行
            temp_signal = calculate_trade_score(symbol, '1h', df_indicators, side, GLOBAL_MACRO_CONTEXT)
            
            if temp_signal is None:
                logging.warning(f"⚠️ {symbol}: SL/TPの再計算に失敗しました。スキップします。")
                continue

            recalculated_sl = temp_signal['stop_loss']
            recalculated_tp = temp_signal['take_profit']
            
            # ポジションを更新
            position['stop_loss'] = recalculated_sl
            position['take_profit'] = recalculated_tp
            
            # 💡 SL/TP注文を再設定
            await _re_set_sl_tp_orders(position, recalculated_sl, recalculated_tp)
            
            logging.info(f"✅ {symbol}: SL/TPを再設定しました (SL: {format_price(recalculated_sl)} / TP: {format_price(recalculated_tp)})")
            
        
        # 1-2. SL/TPトリガーチェック (ローカルチェック - 取引所のSL/TP注文が優先)
        trigger_type = None
        
        if side == 'long':
            # ロングの損切り: 現在価格がSL以下になった
            if current_price <= position['stop_loss']:
                trigger_type = 'Stop_Loss'
            # ロングの利確: 現在価格がTP以上になった
            elif current_price >= position['take_profit']:
                trigger_type = 'Take_Profit'
                
        elif side == 'short':
            # ショートの損切り: 現在価格がSL以上になった
            if current_price >= position['stop_loss']:
                trigger_type = 'Stop_Loss'
            # ショートの利確: 現在価格がTP以下になった
            elif current_price <= position['take_profit']:
                trigger_type = 'Take_Profit'
                
        if trigger_type:
            # 決済トリガーを検出 - ポジションのクローズを実行
            positions_to_close.append({
                'position': position,
                'exit_type': trigger_type,
                'exit_price': current_price
            })

    # 2. 決済が必要なポジションを処理
    for close_data in positions_to_close:
        position = close_data['position']
        exit_type = close_data['exit_type']
        exit_price = close_data['exit_price']
        
        # クローズ注文の実行 (Market Order)
        trade_result = await execute_close_position(position, exit_price, exit_type)
        
        # ログと通知
        if trade_result['status'] == 'ok':
            # 決済成功後、ポジションリストから削除
            OPEN_POSITIONS.remove(position)
            
            # 通知のためのデータ整形
            notification_data = position.copy()
            notification_data['trade_result'] = trade_result
            
            # PnL計算
            pnl_usdt, pnl_rate = calculate_pnl(
                entry_price=position['entry_price'], 
                exit_price=exit_price, 
                contracts=position['contracts'], 
                side=position['side'], 
                leverage=position['leverage']
            )
            
            trade_result['pnl_usdt'] = pnl_usdt
            trade_result['pnl_rate'] = pnl_rate
            trade_result['entry_price'] = position['entry_price']
            trade_result['exit_price'] = exit_price
            trade_result['contracts'] = position['contracts']
            trade_result['exit_type'] = exit_type

            current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)

            # ログ記録
            log_signal(notification_data, "ポジション決済", trade_result)
            
            # Telegram通知
            message = format_telegram_message(notification_data, "ポジション決済", current_threshold, trade_result, exit_type)
            await send_telegram_notification(message)
        else:
            logging.error(f"❌ {symbol}: ポジション決済に失敗しました: {trade_result['error_message']}")


async def execute_close_position(position: Dict, exit_price: float, exit_type: str) -> Dict:
    """
    ポジションをマーケット注文でクローズする。
    """
    global EXCHANGE_CLIENT
    
    symbol = position['symbol']
    side = position['side']
    contracts = position['contracts']
    
    if TEST_MODE:
        logging.warning(f"⚠️ TEST_MODE: {symbol} - {side.upper()} ポジションの決済をスキップします。Exit Type: {exit_type}")
        return {
            'status': 'test_mode', 
            'error_message': 'Test mode is active.',
            'filled_amount': abs(contracts),
            'exit_price': exit_price
        }
        
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        return {'status': 'error', 'error_message': 'CCXT Client not ready.'}
        
    # 1. 既存のSL/TP注文を全てキャンセル
    try:
        # ccxtのcancel_all_ordersを試みる
        cancel_result = await EXCHANGE_CLIENT.cancel_all_orders(symbol)
        logging.info(f"✅ {symbol}: 既存のSL/TPを含む全ての注文をキャンセルしました。")
    except Exception as e:
        logging.warning(f"⚠️ {symbol}: 既存注文のキャンセル中にエラーが発生しました: {e}")
        
    
    # 2. ポジションクローズの成行注文
    try:
        order_side = 'sell' if side == 'long' else 'buy'
        # 数量は絶対値で渡す
        absolute_amount = abs(contracts)
        
        # 契約サイズの丸め処理
        amount_to_close = EXCHANGE_CLIENT.amount_to_precision(symbol, absolute_amount)

        # 注文実行
        order = await EXCHANGE_CLIENT.create_market_order(
            symbol=symbol,
            side=order_side,
            amount=float(amount_to_close),
            params={'leverage': LEVERAGE}
        )
        
        if order.get('status') not in ['closed', 'filled']:
            return {'status': 'error', 'error_message': f'Close market order not filled. Status: {order.get("status")}'}
            
        filled_amount = float(order.get('filled', order.get('amount', 0.0)))
        filled_price = float(order.get('average', exit_price))

        return {
            'status': 'ok',
            'filled_amount': filled_amount,
            'exit_price': filled_price,
        }

    except ccxt.DDoSProtection as e:
        logging.error(f"❌ DDoS保護エラー: 決済失敗。{e}")
        return {'status': 'error', 'error_message': f'DDoS Protection: {e}'}
    except ccxt.ExchangeError as e:
        logging.error(f"❌ 取引所エラー: 決済失敗。{e}")
        return {'status': 'error', 'error_message': f'Exchange Error: {e}'}
    except Exception as e:
        logging.error(f"❌ 決済実行中の予期せぬエラー: {e}", exc_info=True)
        return {'status': 'error', 'error_message': f'Unexpected Error: {e}'}


async def _re_set_sl_tp_orders(position: Dict, stop_loss: float, take_profit: float):
    """
    ポジション情報に基づいてSL/TP注文を再設定するヘルパー関数。
    """
    global EXCHANGE_CLIENT
    
    symbol = position['symbol']
    side = position['side']
    filled_amount = abs(position['contracts'])
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        return
        
    try:
        # 既存の注文をキャンセル (特定のSL/TP注文IDがあればそれを使うべきだが、ここでは全キャンセルを試みる)
        await EXCHANGE_CLIENT.cancel_all_orders(symbol)
        logging.info(f"✅ {symbol}: SL/TP再設定のため、既存注文をキャンセルしました。")
    except Exception:
        logging.warning(f"⚠️ {symbol}: 既存注文のキャンセル中にエラーが発生しましたが続行します。")
        
    
    # ストップ注文 (STOP_MARKETを推奨)
    stop_side = 'sell' if side == 'long' else 'buy'
    stop_price = EXCHANGE_CLIENT.price_to_precision(symbol, stop_loss)
    
    try:
        await EXCHANGE_CLIENT.create_order(
            symbol=symbol,
            type='stop_market',
            side=stop_side,
            amount=filled_amount,
            params={
                'stopPrice': stop_price,
                'closeOnTrigger': True
            }
        )
        logging.info(f"✅ {symbol}: SL注文を {stop_price} に再設定しました。")
    except Exception as e:
        logging.error(f"❌ {symbol}: SL注文の再設定に失敗しました: {e}")
    
    # TP注文 (TAKE_PROFIT_MARKETを推奨)
    take_profit_side = 'sell' if side == 'long' else 'buy'
    take_profit_price = EXCHANGE_CLIENT.price_to_precision(symbol, take_profit)
    
    try:
        await EXCHANGE_CLIENT.create_order(
            symbol=symbol,
            type='take_profit_market',
            side=take_profit_side,
            amount=filled_amount,
            params={
                'stopPrice': take_profit_price,
                'closeOnTrigger': True
            }
        )
        logging.info(f"✅ {symbol}: TP注文を {take_profit_price} に再設定しました。")
    except Exception as e:
        logging.error(f"❌ {symbol}: TP注文の再設定に失敗しました: {e}")

def calculate_pnl(entry_price: float, exit_price: float, contracts: float, side: str, leverage: int) -> Tuple[float, float]:
    """
    決済結果のPnL (USDT) とリターン率 (%) を計算する。
    """
    if entry_price <= 0 or abs(contracts) <= 0:
        return 0.0, 0.0
        
    # PnL (USDT) の計算
    if side == 'long':
        pnl_usdt = (exit_price - entry_price) * contracts
    else: # short
        pnl_usdt = (entry_price - exit_price) * abs(contracts) # contractsはマイナス値の可能性もあるためabsを使用

    # 初期証拠金 (ポジションの名目価値 / レバレッジ)
    notional_value = entry_price * abs(contracts)
    initial_margin = notional_value / leverage
    
    if initial_margin <= 0:
        pnl_rate = 0.0
    else:
        # リターン率 (証拠金に対するPnLの割合)
        pnl_rate = pnl_usdt / initial_margin
        
    return pnl_usdt, pnl_rate


# ====================================================================================
# SCHEDULERS
# ====================================================================================

async def main_bot_scheduler():
    """
    メインのBOT実行スケジューラ
    """
    global LAST_SUCCESS_TIME, LAST_SIGNAL_TIME, IS_FIRST_MAIN_LOOP_COMPLETED, CURRENT_MONITOR_SYMBOLS, GLOBAL_MACRO_CONTEXT
    
    logging.info("🤖 メインBOTスケジューラを開始します。")

    # 1. CCXTクライアントの初期化を試みる
    if not await initialize_exchange_client():
        # 致命的なエラー: 初期化に失敗した場合は取引を停止し、通知して終了
        await send_telegram_notification(f"🚨 **Apex BOT 起動失敗** 致命的エラー\nCCXTクライアントの初期化に失敗しました。BOTを終了します。")
        logging.critical("❌ BOTのメインループを停止します。")
        return 

    # 2. 初期セットアップループ
    while True:
        try:
            # マクロコンテキストの更新
            fgi_context = await fetch_fear_and_greed_index()
            GLOBAL_MACRO_CONTEXT.update(fgi_context)
            
            # 口座ステータスの取得とポジション情報の更新
            account_status = await fetch_account_status()
            
            # 出来高TOP銘柄の更新
            CURRENT_MONITOR_SYMBOLS = await fetch_top_volume_symbols()

            # 初期メッセージの通知とログ
            current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
            startup_message = format_startup_message(account_status, GLOBAL_MACRO_CONTEXT, len(CURRENT_MONITOR_SYMBOLS), current_threshold)
            await send_telegram_notification(startup_message)

            IS_FIRST_MAIN_LOOP_COMPLETED = True
            logging.info("✅ BOT初期セットアップが完了しました。メイン分析ループに移行します。")
            break # セットアップが完了したらメインループへ
            
        except Exception as e:
            logging.error(f"❌ BOT初期セットアップ中にエラーが発生しました: {e}。5秒後にリトライします。", exc_info=True)
            await asyncio.sleep(5)
            
    # 3. メイン分析・取引ループ
    while IS_CLIENT_READY: # クライアントが使用可能であればループを継続
        try:
            start_time = time.time()
            
            # a. マクロコンテキストの更新 (毎ループ)
            fgi_context = await fetch_fear_and_greed_index()
            GLOBAL_MACRO_CONTEXT.update(fgi_context)
            
            # b. 口座ステータスとポジションの更新 (毎ループ)
            account_status = await fetch_account_status()
            
            # c. 取引シグナルを検索
            best_signals = await find_best_signals(GLOBAL_MACRO_CONTEXT)
            
            current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
            
            if best_signals:
                for signal in best_signals:
                    symbol = signal['symbol']
                    
                    # 既にポジションがある銘柄はスキップ
                    if any(p['symbol'] == symbol for p in OPEN_POSITIONS):
                        logging.info(f"ℹ️ {symbol}: 既にポジションがあるため、新規シグナルをスキップします。")
                        continue
                        
                    # 取引を実行
                    trade_result = await execute_trade(signal)
                    
                    # 実行結果をシグナルに統合し、通知とログを記録
                    signal['trade_result'] = trade_result
                    
                    message = format_telegram_message(signal, "取引シグナル", current_threshold, trade_result)
                    
                    if trade_result['status'] in ['ok', 'test_mode']:
                        # 成功した場合、最終取引時間を更新し、新しいポジションをOPEN_POSITIONSに追加
                        LAST_SIGNAL_TIME[symbol] = time.time()
                        LAST_SUCCESS_TIME = time.time()
                        
                        if trade_result['status'] == 'ok':
                            # 実行した取引の詳細をポジションオブジェクトとして追加
                            new_position = {
                                'symbol': symbol,
                                'side': signal['side'],
                                'contracts': trade_result['filled_amount'] if signal['side'] == 'long' else -trade_result['filled_amount'],
                                'entry_price': trade_result['entry_price'],
                                'filled_usdt': trade_result['filled_usdt'],
                                'liquidation_price': signal['liquidation_price'],
                                'stop_loss': signal['stop_loss'],
                                'take_profit': signal['take_profit'],
                                'leverage': LEVERAGE,
                                'raw_info': {},
                                'id': str(uuid.uuid4())
                            }
                            OPEN_POSITIONS.append(new_position)
                            
                        log_signal(signal, "取引成功", trade_result)
                    else:
                        log_signal(signal, "取引失敗", trade_result)
                        
                    await send_telegram_notification(message)
                    
                    # 💡 レートリミット対策として、取引実行後は少し待機
                    await asyncio.sleep(LEVERAGE_SETTING_DELAY * 2) 

            else:
                logging.info("ℹ️ 今回の分析で、取引閾値を超えるシグナルは見つかりませんでした。")

            # d. 分析結果の定期通知 (取引シグナルが出なかった場合に実行)
            await notify_highest_analysis_score()
            
            # e. WebShareへのデータアップロード (定期実行)
            await check_and_send_webshare_update(account_status)
            
            # f. ループ終了処理と待機
            elapsed_time = time.time() - start_time
            sleep_duration = max(0.0, LOOP_INTERVAL - elapsed_time)
            logging.info(f"✅ メイン分析ループ完了。処理時間: {elapsed_time:.2f}秒。{sleep_duration:.0f}秒待機します。")
            await asyncio.sleep(sleep_duration)
            
        except Exception as e:
            logging.error(f"❌ メインBOTループでエラーが発生しました: {e}", exc_info=True)
            # 致命的なエラーの可能性もあるため、クライアントを再初期化してリトライを試みる
            await initialize_exchange_client()
            await asyncio.sleep(MONITOR_INTERVAL) # 短いインターバルでリトライ

async def check_and_send_webshare_update(account_status: Dict):
    """WebShareの更新間隔をチェックし、必要であればデータを送信する。"""
    global LAST_WEBSHARE_UPLOAD_TIME, WEBSHARE_UPLOAD_INTERVAL
    
    current_time = time.time()
    if current_time - LAST_WEBSHARE_UPLOAD_TIME < WEBSHARE_UPLOAD_INTERVAL:
        return
        
    try:
        data_for_webshare = {
            'timestamp_jst': datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
            'bot_version': BOT_VERSION,
            'exchange': CCXT_CLIENT_NAME.upper(),
            'equity_usdt': account_status.get('total_usdt_balance', 0.0),
            'open_positions_count': len(OPEN_POSITIONS),
            'open_positions': OPEN_POSITIONS,
            'macro_context': GLOBAL_MACRO_CONTEXT,
            'last_success_time_jst': datetime.fromtimestamp(LAST_SUCCESS_TIME, JST).strftime("%Y-%m-%d %H:%M:%S") if LAST_SUCCESS_TIME > 0 else 'N/A',
            'last_signals': LAST_ANALYSIS_SIGNALS[:5], # 直近のトップシグナルを送信
        }
        
        await send_webshare_update(data_for_webshare)
        LAST_WEBSHARE_UPLOAD_TIME = current_time
        
    except Exception as e:
        logging.error(f"❌ WebShareデータ準備/送信中にエラーが発生しました: {e}", exc_info=True)


async def position_monitor_scheduler():
    """
    ポジションのSL/TPチェックと決済処理をメインループとは独立して監視するスケジューラ。
    """
    logging.info("🕵️ ポジション監視スケジューラを開始します。")
    
    # メインループの初期化完了を待つ
    while not IS_FIRST_MAIN_LOOP_COMPLETED:
        await asyncio.sleep(1)
        
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
        logging.error(f"🌐 FastAPIで捕捉されなかった例外が発生しました: {exc}", exc_info=True)
        
    return JSONResponse(
        status_code=500,
        content={"message": f"Internal Server Error: {exc}"},
    )


if __name__ == "__main__":
    # uvicornでFastAPIアプリケーションを起動
    uvicorn.run(
        "main_render__29:app", 
        host="0.0.0.0", 
        port=8000, 
        log_level="info", 
        reload=False
    )
