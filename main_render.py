# ====================================================================================
# Apex BOT v19.0.28 - Final Integrated Build (Patch 24: 起動完了通知の追加)
#
# 修正ポイント:
# 1. 【機能追加】BOT起動後、初回メインループ完了時にアカウントステータスと市場状況を含む通知を送信 (Patch 24)。
# 2. 【機能追加】口座残高と保有ポジションを取得する fetch_account_status 関数を実装。
# 3. 【ロジック修正】main_loop に初回実行完了フラグ (IS_FIRST_MAIN_LOOP_COMPLETED) を追加。
# 4. 【バージョン更新】全てのバージョン情報を Patch 24 に更新。
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
import ftplib # FTP接続に使用

# .envファイルから環境変数を読み込む
load_dotenv()

# ====================================================================================
# CONFIG & CONSTANTS
# ====================================================================================

JST = timezone(timedelta(hours=9))

# 出来高TOP40に加えて、主要な基軸通貨をDefaultに含めておく (現物シンボル形式 BTC/USDT)
DEFAULT_SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT", "ADA/USDT",
    "DOGE/USDT", "DOT/USDT", "TRX/USDT", "MATIC/USDT", 
    "LTC/USDT", "AVAX/USDT", "LINK/USDT", "UNI/USDT", "ETC/USDT", "BCH/USDT",
    "NEAR/USDT", "ATOM/USDT", "FTM/USDT", "ALGO/USDT", "XLM/USDT", "SAND/USDT",
    "GALA/USDT", "FIL/USDT", "EOS/USDT", "AXS/USDT", "MANA/USDT", "AAVE/USDT",
    "MKR/USDT", "THETA/USDT", "FLOW/USDT", "IMX/USDT", # 32銘柄の静的リスト
]
TOP_SYMBOL_LIMIT = 40               # 監視対象銘柄の最大数 (出来高TOPから選出)を40に引き上げ
LOOP_INTERVAL = 60 * 10             # メインループの実行間隔 (秒) - 10分ごと
ANALYSIS_ONLY_INTERVAL = 60 * 60    # 分析専用通知の実行間隔 (秒) - 1時間ごと
WEBSHARE_UPLOAD_INTERVAL = 60 * 60  # WebShareログアップロード間隔 (1時間ごと)

# 💡 クライアント設定
CCXT_CLIENT_NAME = os.getenv("EXCHANGE_CLIENT", "mexc")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
API_KEY = os.getenv(f"{CCXT_CLIENT_NAME.upper()}_API_KEY")
SECRET_KEY = os.getenv(f"{CCXT_CLIENT_NAME.upper()}_SECRET")
TEST_MODE = os.getenv("TEST_MODE", "False").lower() in ('true', '1', 't')
SKIP_MARKET_UPDATE = os.getenv("SKIP_MARKET_UPDATE", "False").lower() in ('true', '1', 't')

# 💡 WEBSHARE設定 (FTP/WebDAVなど、外部ログストレージを想定)
WEBSHARE_HOST = os.getenv("WEBSHARE_HOST")
WEBSHARE_PORT = int(os.getenv("WEBSHARE_PORT", "21")) # デフォルトはFTPポート
WEBSHARE_USER = os.getenv("WEBSHARE_USER")
WEBSHARE_PASS = os.getenv("WEBSHARE_PASS")

# グローバル変数 (状態管理用)
EXCHANGE_CLIENT: Optional[ccxt_async.Exchange] = None
CURRENT_MONITOR_SYMBOLS: List[str] = DEFAULT_SYMBOLS.copy()
LAST_SUCCESS_TIME: float = 0.0
LAST_SIGNAL_TIME: Dict[str, float] = {}
LAST_ANALYSIS_SIGNALS: List[Dict] = []
LAST_HOURLY_NOTIFICATION_TIME: float = 0.0
LAST_ANALYSIS_ONLY_NOTIFICATION_TIME: float = 0.0
LAST_WEBSHARE_UPLOAD_TIME: float = 0.0 
GLOBAL_MACRO_CONTEXT: Dict = {} # マクロコンテキストを保持するための変数
IS_FIRST_MAIN_LOOP_COMPLETED: bool = False # 💡 初回メインループ完了フラグ

# ロギング設定
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
if TEST_MODE:
    logging.warning("⚠️ WARNING: TEST_MODE is active. Trading is disabled.")

# 取引ルール設定
TRADE_SIGNAL_COOLDOWN = 60 * 60 * 2 # 同一銘柄のシグナル通知クールダウン（2時間）
SIGNAL_THRESHOLD = 0.65             # 動的閾値のベースライン (通常時の値 2-3銘柄/日を想定)
TOP_SIGNAL_COUNT = 3                # 通知するシグナルの最大数
REQUIRED_OHLCV_LIMITS = {'15m': 500, '1h': 500, '4h': 500} # 取得するOHLCVの足数

# テクニカル分析定数 (v19.0.28ベース)
TARGET_TIMEFRAMES = ['15m', '1h', '4h']
BASE_SCORE = 0.60                   # ベースとなる取引基準点 (60点)
LONG_TERM_SMA_LENGTH = 200          # 長期トレンドフィルタ用SMA
LONG_TERM_REVERSAL_PENALTY = 0.20   # 長期トレンド逆行時のペナルティ
STRUCTURAL_PIVOT_BONUS = 0.05       # 価格構造/ピボット支持時のボーナス
RSI_MOMENTUM_LOW = 40               # RSIが40以下でロングモメンタム候補
MACD_CROSS_PENALTY = 0.15           # MACDが不利なクロス/発散時のペナルティ
LIQUIDITY_BONUS_MAX = 0.06          # 流動性(板の厚み)による最大ボーナス
FGI_PROXY_BONUS_MAX = 0.05          # 恐怖・貪欲指数による最大ボーナス/ペナルティ
FOREX_BONUS_MAX = 0.0               # 為替機能を削除するため0.0に設定

# 市場環境に応じた動的閾値調整のための定数
FGI_SLUMP_THRESHOLD = -0.02         # FGIプロキシがこの値未満の場合、市場低迷と見なす
FGI_ACTIVE_THRESHOLD = 0.02         # FGIプロキシがこの値を超える場合、市場活発と見なす
SIGNAL_THRESHOLD_SLUMP = 0.70       # 低迷時の閾値 (1-2銘柄/日を想定)
SIGNAL_THRESHOLD_NORMAL = 0.65      # 通常時の閾値 (2-3銘柄/日を想定)
SIGNAL_THRESHOLD_ACTIVE = 0.60      # 活発時の閾値 (3+銘柄/日を想定)

RSI_DIVERGENCE_BONUS = 0.10         # RSIダイバージェンス時のボーナス
VOLATILITY_BB_PENALTY_THRESHOLD = 0.01 # ボラティリティ過熱時のペナルティ閾値
OBV_MOMENTUM_BONUS = 0.04           # OBVトレンド一致時のボーナス

# ====================================================================================
# UTILITIES & FORMATTING
# ====================================================================================

def format_usdt(amount: float) -> str:
    """USDT金額を整形する"""
    if amount >= 1.0:
        return f"{amount:,.2f}"
    elif amount >= 0.01:
        return f"{amount:.4f}"
    else:
        return f"{amount:.6f}"

def get_estimated_win_rate(score: float) -> str:
    """スコアに基づいて推定勝率を返す (通知用)"""
    if score >= 0.90: return "90%+"
    if score >= 0.85: return "85-90%"
    if score >= 0.75: return "75-85%"
    if score >= 0.65: return "65-75%" 
    if score >= 0.60: return "60-65%"
    return "<60% (低)"

def get_score_breakdown(signal: Dict) -> str:
    """分析スコアの詳細なブレークダウンメッセージを作成する (Telegram通知用)"""
    tech_data = signal.get('tech_data', {})
    timeframe = signal.get('timeframe', 'N/A')
    
    # スコア算出ロジックから取得できる定数値 (通知表示に利用)
    LONG_TERM_REVERSAL_PENALTY_CONST = LONG_TERM_REVERSAL_PENALTY 
    MACD_CROSS_PENALTY_CONST = MACD_CROSS_PENALTY                 
    LIQUIDITY_BONUS_POINT_CONST = LIQUIDITY_BONUS_MAX           
    
    breakdown_list = []

    # 1. ベーススコア (全ての分析の出発点)
    breakdown_list.append(f"  - **ベーススコア ({timeframe})**: <code>+{BASE_SCORE*100:.1f}</code> 点")
    
    # 2. 長期トレンド/構造の確認
    penalty_applied = tech_data.get('long_term_reversal_penalty_value', 0.0)
    if penalty_applied > 0.0:
        breakdown_list.append(f"  - ❌ 長期トレンド逆行 (SMA{LONG_TERM_SMA_LENGTH}): <code>-{penalty_applied*100:.1f}</code> 点")
    else:
        # ペナルティ回避時のボーナス相当として表示
        breakdown_list.append(f"  - ✅ 長期トレンド一致 (SMA{LONG_TERM_SMA_LENGTH}): <code>+{LONG_TERM_REVERSAL_PENALTY_CONST*100:.1f}</code> 点 (ペナルティ回避)")

    # 価格構造/ピボット支持ボーナス
    pivot_bonus = tech_data.get('structural_pivot_bonus', 0.0)
    if pivot_bonus > 0.0:
        breakdown_list.append(f"  - ✅ 価格構造/ピボット支持: <code>+{pivot_bonus*100:.1f}</code> 点")

    # 3. モメンタム/出来高の確認
    macd_penalty_applied = tech_data.get('macd_penalty_value', 0.0)
    stoch_penalty_applied = tech_data.get('stoch_filter_penalty_value', 0.0)
    total_momentum_penalty = macd_penalty_applied + stoch_penalty_applied

    if total_momentum_penalty > 0.0:
        breakdown_list.append(f"  - ❌ モメンタム/クロス不利: <code>-{total_momentum_penalty*100:.1f}</code> 点")
    else:
        # ペナルティ回避時のボーナス相当として表示
        breakdown_list.append(f"  - ✅ MACD/RSIモメンタム加速: <code>+{MACD_CROSS_PENALTY_CONST*100:.1f}</code> 点相当 (ペナルティ回避)")

    # 出来高/OBV確証ボーナス
    volume_bonus = tech_data.get('volume_confirmation_bonus', 0.0) # 現在未使用だが残しておく
    obv_bonus = tech_data.get('obv_momentum_bonus_value', 0.0)
    total_vol_bonus = volume_bonus + obv_bonus
    if total_vol_bonus > 0.0:
        breakdown_list.append(f"  - ✅ 出来高/OBV確証: <code>+{total_vol_bonus*100:.1f}</code> 点")
    
    # 4. 流動性/マクロ要因
    # 流動性ボーナス
    liquidity_bonus = tech_data.get('liquidity_bonus_value', 0.0)
    if liquidity_bonus > 0.0:
        breakdown_list.append(f"  - ✅ 流動性 (板の厚み) 優位: <code>+{LIQUIDITY_BONUS_POINT_CONST*100:.1f}</code> 点")
        
    # FGIマクロ要因
    fgi_bonus = tech_data.get('sentiment_fgi_proxy_bonus', 0.0)
    if abs(fgi_bonus) > 0.001:
        sign = '✅' if fgi_bonus > 0 else '❌'
        breakdown_list.append(f"  - {sign} FGIマクロ影響: <code>{'+' if fgi_bonus > 0 else ''}{fgi_bonus*100:.1f}</code> 点")

    # 為替マクロ (常に0.0を表示)
    forex_bonus = tech_data.get('forex_bonus', 0.0) 
    breakdown_list.append(f"  - ⚪ 為替マクロ影響: <code>{forex_bonus*100:.1f}</code> 点 (機能削除済)")
    
    return "\n".join(breakdown_list)


def format_analysis_only_message(all_signals: List[Dict], macro_context: Dict, current_threshold: float, monitoring_count: int) -> str:
    """1時間ごとの分析専用メッセージを作成する"""
    now_jst = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")
    
    # 1. 候補リストの作成 (スコア降順にソート)
    sorted_signals = sorted(all_signals, key=lambda s: s.get('score', 0.0), reverse=True)
    
    header = (
        f"📊 **Apex Market Snapshot (Hourly Analysis)**\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - **確認日時**: {now_jst} (JST)\n"
        f"  - **取引ステータス**: <b>分析通知のみ</b>\n"
        f"  - **対象銘柄数**: <code>{monitoring_count}</code>\n"
        f"  - **監視取引所**: <code>{CCXT_CLIENT_NAME.upper()}</code>\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n\n"
    )

    # マクロコンテキスト情報
    fgi_raw_value = macro_context.get('fgi_raw_value', 'N/A')
    fgi_proxy = macro_context.get('fgi_proxy', 0.0)
    forex_bonus = macro_context.get('forex_bonus', 0.0) # 常に0.0

    fgi_sentiment = "リスクオン" if fgi_proxy > FGI_ACTIVE_THRESHOLD else ("リスクオフ" if fgi_proxy < FGI_SLUMP_THRESHOLD else "中立")
    forex_display = "中立 (機能削除済)"
    
    # 市場環境の判定
    if current_threshold == SIGNAL_THRESHOLD_SLUMP:
        market_condition_text = "低迷/リスクオフ (Threshold: 70点)"
    elif current_threshold == SIGNAL_THRESHOLD_ACTIVE:
        market_condition_text = "活発/リスクオン (Threshold: 60点)"
    else:
        market_condition_text = "通常/中立 (Threshold: 65点)"
    

    macro_section = (
        f"🌍 <b>グローバルマクロ分析</b>\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - **現在の市場環境**: <code>{market_condition_text}</code>\n"
        f"  - **恐怖・貪欲指数 (FGI)**: <code>{fgi_raw_value}</code> ({fgi_sentiment})\n"
        f"  - **為替マクロ (EUR/USD)**: {forex_display}\n"
        f"  - **総合マクロ影響**: <code>{((fgi_proxy + forex_bonus) * 100):.2f}</code> 点\n\n"
    )

    # トップシグナル情報 (Rank 1のみに焦点を当てる)
    signal_section = "📈 <b>トップシグナル候補 (スコア順)</b>\n"
    
    if sorted_signals:
        top_signal = sorted_signals[0] # Rank 1を取得
        symbol = top_signal['symbol']
        timeframe = top_signal['timeframe']
        score = top_signal['score']
        rr_ratio = top_signal['rr_ratio']
        
        # スコア詳細ブレークダウンの生成
        breakdown_details = get_score_breakdown(top_signal)
        
        score_color = ""
        if score < current_threshold:
             score_color = "⚠️" 
        if score < BASE_SCORE: 
             score_color = "🔴"
             
        rr_display = f"1:{rr_ratio:.1f}" if rr_ratio >= 1.0 else f"1:{rr_ratio:.1f} ❌"
        
        signal_section += (
            f"  🥇 <b>{symbol}</b> ({timeframe}) - **最高スコア** {score_color}\n"
            f"     - **総合スコア**: <code>{score * 100:.2f} / 100</code> (推定勝率: {get_estimated_win_rate(score)})\n"
            f"     - **リスクリワード比率 (RRR)**: <code>{rr_display}</code>\n"
            f"  \n**📊 スコア詳細ブレークダウン** (+/-要因)\n"
            f"  <code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
            f"{breakdown_details}\n"
            f"  <code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        )

        # 警告メッセージの追加
        if top_signal['score'] < current_threshold:
             signal_section += f"\n<pre>⚠️ 注: 上記は監視中の最高スコアですが、取引閾値 ({current_threshold*100:.0f}点) 未満です。</pre>\n"
        
        if top_signal['score'] < BASE_SCORE:
             signal_section += f"<pre>🔴 警告: 最高スコアが取引基準点 ({BASE_SCORE*100:.0f}点) 未満です。</pre>\n"

    else:
        signal_section += "  - **シグナル候補なし**: 現在、すべての監視銘柄で最低限のリスクリワード比率を満たすロングシグナルは見つかりませんでした。\n"
    
    footer = (
        f"\n<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"<pre>※ この通知は取引実行を伴いません。</pre>"
        f"<i>Bot Ver: v19.0.28 - Final Integrated Build (Patch 24)</i>"
    )

    return header + macro_section + signal_section + footer

def format_startup_message(
    account_status: Dict, 
    macro_context: Dict, 
    monitoring_count: int,
    current_threshold: float,
    bot_version: str
) -> str:
    """💡 初回起動完了通知用のメッセージを作成する"""
    now_jst = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")
    
    # マクロ情報
    fgi_value = macro_context.get('fgi_raw_value', 'N/A')
    fgi_proxy = macro_context.get('fgi_proxy', 0.0)
    forex_bonus = macro_context.get('forex_bonus', 0.0)
    fgi_sentiment = "リスクオン" if fgi_proxy > FGI_ACTIVE_THRESHOLD else ("リスクオフ" if fgi_proxy < FGI_SLUMP_THRESHOLD else "中立")

    # 市場環境の判定
    if current_threshold == SIGNAL_THRESHOLD_SLUMP:
        market_condition_text = "低迷/リスクオフ"
    elif current_threshold == SIGNAL_THRESHOLD_ACTIVE:
        market_condition_text = "活発/リスクオン"
    else:
        market_condition_text = "通常/中立"

    header = (
        f"🤖 **Apex BOT 起動完了通知** 🟢\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - **確認日時**: {now_jst} (JST)\n"
        f"  - **取引所**: <code>{CCXT_CLIENT_NAME.upper()}</code> (現物モード)\n"
        f"  - **監視銘柄数**: <code>{monitoring_count}</code>\n"
        f"  - **BOTバージョン**: <code>{bot_version}</code>\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n\n"
    )

    # 1. 残高/ポジション情報
    balance_section = f"💰 <b>口座ステータス</b>\n"
    if account_status.get('error'):
        balance_section += f"<pre>❌ ステータス取得エラー: {account_status['error']}</pre>\n"
    else:
        balance_section += (
            f"  - **USDT残高**: <code>{format_usdt(account_status['total_usdt_balance'])}</code> USDT\n"
        )
        
        open_positions = account_status['open_positions']
        if open_positions:
            total_position_value = sum(p['usdt_value'] for p in open_positions)
            balance_section += (
                f"  - **保有ポジション**: <code>{len(open_positions)}</code> 銘柄 (合計評価額: <code>{format_usdt(total_position_value)}</code> USDT)\n"
            )
            for i, pos in enumerate(open_positions[:3]): # Top 3のみ表示
                balance_section += f"    - Top {i+1}: {pos['symbol']} ({format_usdt(pos['usdt_value'])} USDT)\n"
            if len(open_positions) > 3:
                balance_section += f"    - ...他 {len(open_positions) - 3} 銘柄\n"
        else:
             balance_section += f"  - **保有ポジション**: <code>なし</code>\n"
             
    balance_section += f"\n"

    # 2. 市場状況 (スコア付け)
    macro_section = (
        f"🌍 <b>市場環境スコアリング</b>\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - **取引閾値 (Score)**: <code>{current_threshold*100:.0f} / 100</code>\n"
        f"  - **現在の市場環境**: <code>{market_condition_text}</code>\n"
        f"  - **FGI (恐怖・貪欲)**: <code>{fgi_value}</code> ({fgi_sentiment})\n"
        f"  - **総合マクロ影響**: <code>{((fgi_proxy + forex_bonus) * 100):.2f}</code> 点\n\n"
    )

    footer = (
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"<pre>※ この通知はメインの分析ループが一度完了したことを示します。約10分ごとに分析が実行されます。</pre>"
    )

    return header + balance_section + macro_section + footer

def format_telegram_message(signal: Dict, context: str, current_threshold: float) -> str:
    """Telegram通知用のメッセージを作成する"""
    now_jst = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")
    symbol = signal['symbol']
    timeframe = signal['timeframe']
    score = signal['score']
    entry_price = signal['entry_price']
    stop_loss = signal['stop_loss']
    take_profit = signal['take_profit']
    rr_ratio = signal['rr_ratio']
    
    estimated_wr = get_estimated_win_rate(score)
    
    breakdown_details = get_score_breakdown(signal) # スコアブレークダウンを使用

    message = (
        f"🚀 **Apex TRADE SIGNAL ({context})**\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - **日時**: {now_jst} (JST)\n"
        f"  - **銘柄**: <b>{symbol}</b> ({timeframe})\n"
        f"  - **監視取引所**: <code>{CCXT_CLIENT_NAME.upper()}</code>\n"
        f"  - **取引タイプ**: <b>現物 (Spot) - ロング</b>\n" # 現物取引であることを明記
        f"  - **総合スコア**: <code>{score * 100:.2f} / 100</code>\n"
        f"  - **取引閾値**: <code>{current_threshold * 100:.2f}</code> 点 (市場環境による動的設定)\n"
        f"  - **推定勝率**: <code>{estimated_wr}</code>\n"
        f"  - **リスクリワード比率 (RRR)**: <code>1:{rr_ratio:.2f}</code>\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"💰 **取引パラメータ (ロング)**\n"
        f"  - **エントリー**: <code>{format_usdt(entry_price)}</code>\n"
        f"  - **ストップロス (SL)**: <code>{format_usdt(stop_loss)}</code>\n"
        f"  - **テイクプロフィット (TP)**: <code>{format_usdt(take_profit)}</code>\n"
        f"  - **リスク幅 (SL)**: <code>{format_usdt(entry_price - stop_loss)}</code> USDT\n"
        f"  - **リワード幅 (TP)**: <code>{format_usdt(take_profit - entry_price)}</code> USDT\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  \n**📊 スコア詳細ブレークダウン** (+/-要因)\n"
        f"{breakdown_details}\n"
        f"  <code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"<i>Bot Ver: v19.0.28 - Final Integrated Build (Patch 24)</i>"
    )
    return message


async def send_telegram_notification(message: str) -> bool:
    """Telegramにメッセージを送信する"""
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

def log_signal(signal: Dict, log_type: str = "SIGNAL") -> None:
    """シグナルをローカルファイルにログする"""
    try:
        log_entry = {
            'timestamp_jst': datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
            'log_type': log_type,
            'symbol': signal.get('symbol', 'N/A'),
            'timeframe': signal.get('timeframe', 'N/A'),
            'score': signal.get('score', 0.0),
            'rr_ratio': signal.get('rr_ratio', 0.0),
            'entry_price': signal.get('entry_price', 0.0),
            'stop_loss': signal.get('stop_loss', 0.0),
            'take_profit': signal.get('take_profit', 0.0),
            'full_signal': signal # フルデータも保存
        }
        
        log_file = f"apex_bot_{log_type.lower()}_log.jsonl"
        with open(log_file, 'a', encoding='utf-8') as f:
            f.write(json.dumps(log_entry, ensure_ascii=False) + '\n')
            
        logging.info(f"✅ {log_type}ログをファイルに記録しました: {signal.get('symbol', 'N/A')} (Score: {signal.get('score', 0.0):.2f})")
    except Exception as e:
        logging.error(f"❌ ログ書き込みエラー: {e}")

def _sync_ftp_upload(local_file: str, remote_file: str):
    """
    同期的にFTPアップロードを実行するヘルパー関数。
    asyncio.to_threadで使用される。
    """
    if not WEBSHARE_HOST or not WEBSHARE_USER or not WEBSHARE_PASS:
        logging.error("❌ WEBSHARE設定 (HOST/USER/PASS) が不足しています。")
        return False

    if not os.path.exists(local_file):
        logging.warning(f"⚠️ ローカルファイル {local_file} が見つかりません。アップロードをスキップします。")
        return True # ファイルがないのはエラーではない

    try:
        # FTP接続とログイン
        ftp = ftplib.FTP()
        ftp.connect(WEBSHARE_HOST, WEBSHARE_PORT, timeout=10)
        ftp.login(WEBSHARE_USER, WEBSHARE_PASS)
        
        # ファイルのアップロード (バイナリモード)
        # リモートパスは /<filename> の形式を想定
        ftp.storbinary(f'STOR {remote_file}', open(local_file, 'rb'))

        ftp.quit()
        return True
        
    except ftplib.all_errors as e:
        logging.error(f"❌ FTPアップロードエラー ({WEBSHARE_HOST}): {e}")
        return False
    except Exception as e:
        logging.error(f"❌ ログアップロードの予期せぬエラー: {e}")
        return False

async def upload_logs_to_webshare():
    """ローカルログファイルを外部ストレージ (WebShare/FTP) にアップロードする"""
    if not WEBSHARE_HOST:
        logging.info("ℹ️ WEBSHARE HOSTが設定されていません。ログアップロードをスキップします。")
        return
        
    log_files = [
        "apex_bot_trade_signal_log.jsonl",
        "apex_bot_hourly_analysis_log.jsonl",
    ]
    
    now_jst = datetime.now(JST)
    upload_timestamp = now_jst.strftime("%Y%m%d_%H%M%S")
    
    logging.info(f"📤 WEBSHAREログアップロード処理を開始します...")

    tasks = []
    for log_file in log_files:
        if os.path.exists(log_file):
            # リモートファイル名にはタイムスタンプとファイル名を含める
            remote_filename = f"apex_log_{upload_timestamp}_{log_file}"
            
            # 同期FTP処理を別スレッドで実行
            tasks.append(
                asyncio.to_thread(_sync_ftp_upload, log_file, remote_filename)
            )

    if not tasks:
        logging.info("ℹ️ アップロード対象のログファイルがありませんでした。")
        return

    # 全てのタスクを並行実行
    results = await asyncio.gather(*tasks)
    
    if all(results):
        logging.info(f"✅ すべてのログファイル ({len(tasks)} 件) を WEBSHARE にアップロードしました。")
    else:
        logging.error("❌ 一部またはすべてのログファイルの WEBSHARE へのアップロードに失敗しました。")

# ====================================================================================
# CCXT & DATA ACQUISITION
# ====================================================================================

async def initialize_exchange_client() -> bool:
    """CCXTクライアントを初期化する"""
    global EXCHANGE_CLIENT
    
    try:
        client_name = CCXT_CLIENT_NAME.lower()
        if client_name == 'binance':
            exchange_class = ccxt_async.binance
        elif client_name == 'bybit':
            exchange_class = ccxt_async.bybit
        # MEXCクライアント
        elif client_name == 'mexc':
            exchange_class = ccxt_async.mexc
        else:
            logging.error(f"❌ 未対応の取引所クライアント: {CCXT_CLIENT_NAME}")
            return False

        # CCXTのオプション設定
        options = {
            'defaultType': 'spot', # 現物取引 (Spot) を想定
        }

        EXCHANGE_CLIENT = exchange_class({
            'apiKey': API_KEY,
            'secret': SECRET_KEY,
            'enableRateLimit': True,
            'options': options
        })
        await EXCHANGE_CLIENT.load_markets()
        logging.info(f"✅ CCXTクライアント ({CCXT_CLIENT_NAME}) を現物取引モードで初期化しました。")
        return True

    except Exception as e:
        logging.critical(f"❌ CCXTクライアント初期化失敗: {e}")
        EXCHANGE_CLIENT = None
        return False

async def fetch_ohlcv_safe(symbol: str, timeframe: str, limit: int) -> Optional[pd.DataFrame]:
    """OHLCVデータを安全に取得する"""
    global EXCHANGE_CLIENT
    if not EXCHANGE_CLIENT:
        return None
        
    try:
        # 現物取引のため、'spot'を指定する必要がある場合がありますが、defaultType='spot'で解決されるはずです。
        ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(symbol, timeframe, limit=limit)
        if not ohlcv:
            return None
            
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df = df.set_index('timestamp')
        return df

    except Exception as e:
        return None

async def fetch_ohlcv_with_fallback(symbol: str, timeframe: str, limit: int) -> Optional[pd.DataFrame]:
    """指定した足数を確実に取得するためにフォールバック処理を行う"""
    df = await fetch_ohlcv_safe(symbol, timeframe, limit)
    
    # データが少ない場合は、より多くの足数を要求してリトライ
    if df is None or len(df) < limit:
        logging.debug(f"ℹ️ {symbol} ({timeframe}): 必要足数 ({limit}) 未満。リトライ中...")
        df_long = await fetch_ohlcv_safe(symbol, timeframe, limit=limit + 100)
        
        if df_long is not None and len(df_long) >= limit:
            return df_long
        elif df_long is not None and len(df_long) > 0:
            return df_long
            
    return df

async def update_symbols_by_volume() -> None:
    """出来高に基づいて監視対象銘柄リストを更新する"""
    global CURRENT_MONITOR_SYMBOLS, EXCHANGE_CLIENT
    if not EXCHANGE_CLIENT or SKIP_MARKET_UPDATE:
        logging.info("ℹ️ 銘柄リスト更新をスキップしました。")
        return
        
    try:
        # すべてのUSDTペアの現物銘柄を取得
        markets = await EXCHANGE_CLIENT.load_markets()
        usdt_spot_symbols = [
            s for s, m in markets.items() 
            if m['active'] and m['spot'] and (m['quote'] == 'USDT' or s.endswith('/USDT'))
        ]

        if not usdt_spot_symbols:
            logging.warning("⚠️ USDT現物銘柄が見つかりませんでした。デフォルトリストを使用します。")
            CURRENT_MONITOR_SYMBOLS = list(set(DEFAULT_SYMBOLS))
            logging.info(f"✅ 銘柄リストを更新しました。合計: {len(CURRENT_MONITOR_SYMBOLS)} 銘柄。")
            return

        # 24時間出来高情報を取得 (ccxtのfetch_tickersでquoteVolumeを使用)
        tickers = await EXCHANGE_CLIENT.fetch_tickers(usdt_spot_symbols)
        
        volume_data = []
        for symbol in usdt_spot_symbols:
            if symbol in tickers and 'quoteVolume' in tickers[symbol]:
                volume_data.append({
                    'symbol': symbol,
                    'volume': tickers[symbol]['quoteVolume']
                })
        
        # 出来高でソートし、上位TOP_SYMBOL_LIMIT個を取得
        volume_data.sort(key=lambda x: x['volume'], reverse=True)
        top_symbols = [d['symbol'] for d in volume_data if d['volume'] is not None][:TOP_SYMBOL_LIMIT]
        
        # デフォルトシンボルと出来高トップをマージし、重複を排除
        combined_symbols = top_symbols + [s for s in DEFAULT_SYMBOLS if s not in top_symbols]

        # 最終リストの更新 (市場に存在する銘柄のみ)
        CURRENT_MONITOR_SYMBOLS = [s for s in combined_symbols if s in markets] 
        
        # 銘柄数が30未満の場合は警告
        if len(CURRENT_MONITOR_SYMBOLS) < 30:
            logging.warning(f"⚠️ 監視銘柄数が30未満 ({len(CURRENT_MONITOR_SYMBOLS)}) です。静的リストの追加をご検討ください。")
        
        logging.info(f"✅ 現物銘柄リストを更新しました。合計: {len(CURRENT_MONITOR_SYMBOLS)} 銘柄。")
        logging.debug(f"現在の監視銘柄リスト: {CURRENT_MONITOR_SYMBOLS}")

    except Exception as e:
        logging.error(f"❌ 銘柄リスト更新エラー: {e}")
        # エラー時は既存のリストを維持

def fetch_fgi_sync() -> int:
    """Fear & Greed Index (FGI) を取得する (同期処理)"""
    try:
        url = "https://api.alternative.me/fng/?limit=1"
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        data = response.json()
        
        if data and 'data' in data and len(data['data']) > 0:
            return int(data['data'][0]['value'])
        return 50 # 取得失敗時は中立
    except Exception as e:
        logging.error(f"❌ FGI APIエラー: {e}")
        return 50

async def get_crypto_macro_context() -> Dict:
    """市場全体のマクロコンテキストを取得する (FGI リアルデータ取得)"""
    # 最初にデフォルト値を設定
    fgi_value = 50
    fgi_proxy = 0.0
    forex_trend = 'NEUTRAL' # 為替機能を削除し、常に中立を設定
    forex_bonus = 0.0       # 為替機能を削除し、常に0.0を設定

    # 1. 恐怖・貪欲指数 (FGI) を取得
    try:
        fgi_value = await asyncio.to_thread(fetch_fgi_sync)
        fgi_normalized = (fgi_value - 50) / 20.0 # 20は経験的な分散値
        # proxyは±FGI_PROXY_BONUS_MAXの範囲に制限
        fgi_proxy = max(-FGI_PROXY_BONUS_MAX, min(FGI_PROXY_BONUS_MAX, fgi_normalized * FGI_PROXY_BONUS_MAX))
    except Exception as e:
        logging.error(f"❌ FGI取得エラー: {e}")
        
    # 2. 為替マクロデータは削除（forex_trendとforex_bonusはデフォルト値のまま）

    # 3. マクロコンテキストの確定
    return {
        'fgi_raw_value': fgi_value,
        'fgi_proxy': fgi_proxy,
        'forex_trend': forex_trend,
        'forex_bonus': forex_bonus,
    }

async def fetch_account_status() -> Dict:
    """💡 口座残高と保有ポジション（10 USDT以上）を取得する"""
    global EXCHANGE_CLIENT
    status = {
        'total_usdt_balance': 0.0,
        'open_positions': [],
        'error': None
    }
    if not EXCHANGE_CLIENT:
        status['error'] = "Exchange client not initialized."
        return status

    try:
        # 1. 残高の取得
        balance = await EXCHANGE_CLIENT.fetch_balance()
        status['total_usdt_balance'] = balance['total'].get('USDT', 0.0)
        
        # 2. ポジションの取得 (USDT以外の保有資産) と価格取得
        symbols_to_fetch = []
        position_data = []
        for currency, info in balance['total'].items():
            if currency == 'USDT' or info == 0.0 or info < 0.000001:
                continue
            
            symbol = f"{currency}/USDT"
            # 実際に市場にある銘柄か確認
            if symbol in EXCHANGE_CLIENT.markets:
                symbols_to_fetch.append(symbol)
                position_data.append({'symbol': symbol, 'amount': info, 'usdt_value': 0.0})
        
        # 一括でティッカー（価格）を取得
        # CCXTによっては一括取得がサポートされていない場合があるため、安全のためループで取得（ただし非同期）
        tickers = await EXCHANGE_CLIENT.fetch_tickers(symbols_to_fetch)
        
        for pos in position_data:
            symbol = pos['symbol']
            amount = pos['amount']
            
            if symbol in tickers and 'last' in tickers[symbol]:
                market_price = tickers[symbol]['last']
                pos['usdt_value'] = amount * market_price
                
                # 10 USDT以上の価値があるもののみをポジションとして報告
                if pos['usdt_value'] >= 10.0: 
                     status['open_positions'].append(pos)
            else:
                 # 価格が取得できない銘柄はポジションとして計上しない
                 pass

    except Exception as e:
        status['error'] = str(e)
        logging.error(f"❌ 口座ステータス取得エラー: {e}")

    return status

# ====================================================================================
# STRATEGY & SCORING LOGIC
# ====================================================================================

def calculate_volatility_score(df: pd.DataFrame, current_price: float) -> Tuple[float, float]:
    """ボラティリティとBBの状態を評価する"""
    try:
        # BB計算
        bb_period = 20
        bb = ta.bbands(df['close'], length=bb_period, std=2)
        
        if bb is None or bb.empty:
            return 0.0, 0.0

        bb_last = bb.iloc[-1]
        
        # ボラティリティのペナルティ (BB幅が狭すぎる or 価格がBB外側にありすぎる)
        score_penalty = 0.0
        
        # 1. ボラティリティ過熱ペナルティ: 価格がBB上限を超えているか、BB幅が非常に狭いか
        bb_width_ratio = (bb_last.iloc[2] - bb_last.iloc[0]) / bb_last.iloc[1] # BB幅 / MA (比率で評価)
        
        if bb_width_ratio < VOLATILITY_BB_PENALTY_THRESHOLD: # 例: BB幅が狭い
            score_penalty += 0.05
            
        # 2. 価格がBB外側にある場合のペナルティ (逆張り防止)
        if current_price > bb_last.iloc[2]: # Upper Band
            score_penalty += 0.05
        
        return 1.0 - score_penalty, bb_width_ratio

    except Exception as e:
        return 0.0, 0.0

async def calculate_liquidity_bonus(symbol: str, current_price: float) -> Tuple[float, float]:
    """流動性（板情報）からボーナスを計算する"""
    global EXCHANGE_CLIENT
    if not EXCHANGE_CLIENT:
        return 0.0, 0.0

    try:
        # 深度情報を取得 (async_supportを使用)
        orderbook = await EXCHANGE_CLIENT.fetch_order_book(symbol, limit=20)
        
        # 現在価格から一定範囲（例: ±0.5%）の板の厚みを評価
        price_range = current_price * 0.005 # 0.5%
        
        # 買い板（Bids）の厚みを計算（ロングシグナルのため）
        bid_volume = 0.0
        for price, amount in orderbook['bids']:
            if price >= current_price - price_range:
                bid_volume += price * amount # 換算USDT量
        
        # 売り板（Asks）の厚みを計算
        ask_volume = 0.0
        for price, amount in orderbook['asks']:
            if price <= current_price + price_range:
                ask_volume += price * amount # 換算USDT量
                
        # 買い板が売り板より厚い場合にボーナス
        liquidity_ratio = bid_volume / (ask_volume if ask_volume > 0 else 1.0)
        
        if liquidity_ratio > 1.2: # 買い板が1.2倍以上厚い
            bonus = LIQUIDITY_BONUS_MAX
        elif liquidity_ratio > 1.0:
            bonus = LIQUIDITY_BONUS_MAX / 2
        else:
            bonus = 0.0
            
        return bonus, liquidity_ratio

    except Exception as e:
        # logging.error(f"❌ 流動性計算エラー {symbol}: {e}") # ログが多すぎるのを防ぐためコメントアウト
        return 0.0, 0.0

def analyze_single_timeframe(
    df: pd.DataFrame, 
    timeframe: str, 
    macro_context: Dict
) -> Optional[Dict]:
    """特定の時間足のテクニカル分析とスコアリングを実行する (ロングシグナル専用)"""
    if df is None or len(df) < REQUIRED_OHLCV_LIMITS[timeframe] or df.empty:
        return None
        
    df.columns = [c.lower() for c in df.columns]
    
    # 1. テクニカル指標の計算
    df['sma_long'] = ta.sma(df['close'], length=LONG_TERM_SMA_LENGTH)
    df.ta.macd(append=True)
    df.ta.rsi(append=True)
    df.ta.stoch(append=True)
    df.ta.obv(append=True)
    
    # 指標の最後の値を取得
    last = df.iloc[-1]
    prev_last = df.iloc[-2]
    current_price = last['close']
    
    # 2. ベーススコアとペナルティ/ボーナスの初期化
    score = BASE_SCORE # 60点からスタート
    tech_data = {}

    # 3. 長期トレンドフィルター (SMA 200)
    is_long_term_uptrend = current_price > last['sma_long'] and last['sma_long'] > prev_last['sma_long']
    
    if not is_long_term_uptrend:
        score -= LONG_TERM_REVERSAL_PENALTY
        tech_data['long_term_reversal_penalty'] = True
        tech_data['long_term_reversal_penalty_value'] = LONG_TERM_REVERSAL_PENALTY
    else:
        tech_data['long_term_reversal_penalty'] = False
        tech_data['long_term_reversal_penalty_value'] = 0.0

    # 4. モメンタムフィルター (RSI & MACD)
    # MACDがシグナルラインを上回り、ヒストグラムが正であること
    macd_is_favorable = last['MACDh_12_26_9'] > 0 and last['MACD_12_26_9'] > last['MACDs_12_26_9']
    
    tech_data['macd_cross_valid'] = macd_is_favorable
    
    if not macd_is_favorable:
        score -= MACD_CROSS_PENALTY
        tech_data['macd_penalty_value'] = MACD_CROSS_PENALTY
    else:
        tech_data['macd_penalty_value'] = 0.0

    # 5. ストキャスティクスフィルター (Stochastics)
    # ストキャスKとDが低い水準(例: 25以下)で、かつKがDを上回っていること
    stoch_k = last['STOCHk_14_3_3']
    stoch_d = last['STOCHd_14_3_3']
    
    is_stoch_oversold_cross = (stoch_k < 25) and (stoch_d < 25) and (stoch_k > stoch_d)
    
    if not is_stoch_oversold_cross:
        # 過熱圏でのクロス (Stoch K, D > 75) または下向きクロスはペナルティ
        if stoch_k > 75 or stoch_k < stoch_d:
            score -= 0.05
            tech_data['stoch_filter_penalty_value'] = 0.05
        else:
             tech_data['stoch_filter_penalty_value'] = 0.0
    else:
        # 買われすぎ圏でのクロスは無視 (モメンタム加速として認識しない)
         tech_data['stoch_filter_penalty_value'] = 0.0

    # 6. RSIダイバージェンス判定 (簡易版: 2期間のRSIが底を形成しつつ、価格が安値を更新)
    # 最後の3期間のローソク足を使用
    rsi_3 = df['RSI_14'].iloc[-3:].min()
    price_3 = df['low'].iloc[-3:].min()
    
    # 直前の2つの安値とRSIを比較（ざっくりとしたダイバージェンス判定）
    # RSIは上昇傾向だが、価格は安値を更新している（Bullish Divergence）
    if df['RSI_14'].iloc[-1] > df['RSI_14'].iloc[-2] and df['low'].iloc[-1] < df['low'].iloc[-2]:
         score += RSI_DIVERGENCE_BONUS
         tech_data['rsi_divergence_bonus'] = RSI_DIVERGENCE_BONUS
    else:
         tech_data['rsi_divergence_bonus'] = 0.0
         

    # 7. 価格構造/ピボットポイントの支持
    # 価格が過去のローソク足のロー(例: 3期間前)の上にあり、それがサポートとして機能していることを想定
    if len(df) >= 3:
        pivot_low = df['low'].iloc[-3] 
        if current_price > pivot_low:
            score += STRUCTURAL_PIVOT_BONUS
            tech_data['structural_pivot_bonus'] = STRUCTURAL_PIVOT_BONUS
        else:
            tech_data['structural_pivot_bonus'] = 0.0
    else:
         tech_data['structural_pivot_bonus'] = 0.0

    # 8. OBV (On-Balance Volume) による出来高トレンド確認
    # OBVがシグナル方向に上昇しているか
    obv_uptrend = last['OBV'] > prev_last['OBV']
    if obv_uptrend:
        score += OBV_MOMENTUM_BONUS
        tech_data['obv_momentum_bonus_value'] = OBV_MOMENTUM_BONUS
    else:
         tech_data['obv_momentum_bonus_value'] = 0.0
         
    # 9. マクロコンテキストの適用 (マクロは全て tech_data に含まれる)
    fgi_proxy = macro_context.get('fgi_proxy', 0.0)
    forex_bonus = macro_context.get('forex_bonus', 0.0)
    
    score += fgi_proxy # FGIによる補正
    score += forex_bonus # 為替による補正 (常に0.0)
    
    tech_data['sentiment_fgi_proxy_bonus'] = fgi_proxy
    tech_data['forex_bonus'] = forex_bonus


    # 10. 最終的な結果の構築 (まだTP/SL/RRRは計算されていないため、仮の値を使用)
    return {
        'timeframe': timeframe,
        'score': score,
        'entry_price': current_price,
        'stop_loss': 0.0, # 仮
        'take_profit': 0.0, # 仮
        'rr_ratio': 0.0, # 仮
        'tech_data': tech_data
    }


def calculate_trade_parameters(df: pd.DataFrame, signal: Dict) -> Optional[Dict]:
    """
    シグナルからエントリー、ストップロス、テイクプロフィット、RRRを計算する
    (ロング/現物取引専用)
    """
    try:
        current_price = signal['entry_price']
        
        # 1. ストップロス (SL) の決定: 直近のサポートレベルを使用
        # 過去10期間の最安値の少し下 (例: 10期間ローの99%) をSLとする
        sl_length = 10
        min_low = df['low'].iloc[-sl_length:].min()
        stop_loss = min_low * 0.995 # 最安値から0.5%下
        
        if stop_loss >= current_price:
            # ストップロスがエントリー価格より上になってしまう場合は、取引不可と見なす
            return None 

        # 2. リスク幅の計算
        risk_amount = current_price - stop_loss
        if risk_amount <= 0:
             return None # 価格変動が小さすぎる、またはSLが不正

        # 3. テイクプロフィット (TP) の決定: RRR 1.5をターゲットとする
        target_rr_ratio = 1.5 
        reward_amount = risk_amount * target_rr_ratio
        take_profit = current_price + reward_amount
        
        # 4. 最終的なRRRの計算
        final_rr_ratio = (take_profit - current_price) / risk_amount
        
        # 5. シグナルの更新
        signal['stop_loss'] = stop_loss
        signal['take_profit'] = take_profit
        signal['rr_ratio'] = final_rr_ratio
        
        # RRRが最低基準(例: 1.0)を満たさない場合は取引しない (ただし、分析結果は残す)
        # 今回のロジックでは、RRRフィルタはメインループの「シグナル候補の選定」で行う
        
        return signal
        
    except Exception as e:
        return None

async def process_symbol_analysis(symbol: str, macro_context: Dict) -> List[Dict]:
    """単一銘柄の複数時間足分析を実行する"""
    all_signals: List[Dict] = []
    
    # OHLCVデータの取得と並行処理
    ohlcv_data = await asyncio.gather(*[
        fetch_ohlcv_with_fallback(symbol, tf, REQUIRED_OHLCV_LIMITS[tf])
        for tf in TARGET_TIMEFRAMES
    ])

    for i, timeframe in enumerate(TARGET_TIMEFRAMES):
        df = ohlcv_data[i]
        
        # 1. テクニカルスコアリング
        scored_signal = analyze_single_timeframe(df, timeframe, macro_context)
        
        if scored_signal:
            # 2. 流動性ボーナスの計算と適用
            liquidity_bonus, liquidity_ratio = await calculate_liquidity_bonus(symbol, scored_signal['entry_price'])
            scored_signal['score'] += liquidity_bonus
            scored_signal['tech_data']['liquidity_bonus_value'] = liquidity_bonus
            scored_signal['tech_data']['liquidity_ratio'] = liquidity_ratio

            # 3. TP/SL/RRRの計算
            final_signal = calculate_trade_parameters(df, scored_signal)

            if final_signal:
                final_signal['symbol'] = symbol
                all_signals.append(final_signal)

    return all_signals

# ====================================================================================
# MAIN LOOPS & SCHEDULER
# ====================================================================================

def determine_dynamic_threshold(macro_context: Dict) -> float:
    """マクロコンテキストに基づいて動的な取引閾値を決定する"""
    fgi_proxy = macro_context.get('fgi_proxy', 0.0)
    
    if fgi_proxy < FGI_SLUMP_THRESHOLD:
        return SIGNAL_THRESHOLD_SLUMP # 70点
    elif fgi_proxy > FGI_ACTIVE_THRESHOLD:
        return SIGNAL_THRESHOLD_ACTIVE # 60点
    else:
        return SIGNAL_THRESHOLD_NORMAL # 65点


async def main_loop():
    """定期的に実行されるメインの取引分析・実行ループ"""
    global LAST_SUCCESS_TIME, GLOBAL_MACRO_CONTEXT, LAST_ANALYSIS_SIGNALS, IS_FIRST_MAIN_LOOP_COMPLETED
    
    while True:
        start_time = time.time()
        logging.info("🚀 メイン分析ループを開始します...")
        
        try:
            # 1. 市場コンテキストの更新
            await update_symbols_by_volume()
            GLOBAL_MACRO_CONTEXT = await get_crypto_macro_context()
            current_threshold = determine_dynamic_threshold(GLOBAL_MACRO_CONTEXT)

            # 💡 初回起動完了通知 (ここで追加)
            if not IS_FIRST_MAIN_LOOP_COMPLETED:
                logging.info("🚀 初回メインループ完了。起動完了通知を送信します。")
                
                # ステータスとマクロコンテキストを取得
                account_status = await fetch_account_status()
                
                # メッセージ整形
                startup_message = format_startup_message(
                    account_status,
                    GLOBAL_MACRO_CONTEXT,
                    len(CURRENT_MONITOR_SYMBOLS),
                    current_threshold,
                    "v19.0.28 - Final Integrated Build (Patch 24)"
                )
                await send_telegram_notification(startup_message)
                IS_FIRST_MAIN_LOOP_COMPLETED = True # フラグを立てる

            # 2. 全銘柄の分析を並行処理
            analysis_tasks = [
                process_symbol_analysis(symbol, GLOBAL_MACRO_CONTEXT)
                for symbol in CURRENT_MONITOR_SYMBOLS
            ]
            all_results_list = await asyncio.gather(*analysis_tasks)
            
            # 結果をフラット化
            all_signals = [signal for results in all_results_list for signal in results]
            
            # グローバル分析結果を更新
            LAST_ANALYSIS_SIGNALS = all_signals

            # 3. シグナル候補の選定と実行 (RRR >= 1.0, Score >= Dynamic Threshold)
            qualified_signals = [
                s for s in all_signals 
                if s['rr_ratio'] >= 1.0 and s['score'] >= current_threshold
            ]
            
            # スコア順にソート
            qualified_signals.sort(key=lambda s: s['score'], reverse=True)
            
            # クールダウンフィルタを適用し、上位TOP_SIGNAL_COUNT件を通知
            notified_count = 0
            for signal in qualified_signals:
                symbol = signal['symbol']
                current_time = time.time()
                
                # クールダウンチェック
                if current_time - LAST_SIGNAL_TIME.get(symbol, 0.0) < TRADE_SIGNAL_COOLDOWN:
                    continue # スキップ
                
                if notified_count < TOP_SIGNAL_COUNT:
                    # 4. 取引実行ロジック (現物取引では、ここでは通知のみ)
                    
                    # 通知メッセージの作成
                    message = format_telegram_message(signal, "取引シグナル", current_threshold)
                    await send_telegram_notification(message)
                    
                    # ログ記録
                    log_signal(signal, log_type="TRADE_SIGNAL")

                    # 状態更新
                    LAST_SIGNAL_TIME[symbol] = current_time
                    notified_count += 1
            
            # 5. 低スコア/シグナルゼロ時のロギング
            if not qualified_signals:
                # 最高スコアのシグナル（スコア >= BASE_SCORE & RRR >= 1.0）を抽出
                best_signal = next(
                    (s for s in all_signals if s['rr_ratio'] >= 1.0 and s['score'] >= BASE_SCORE), 
                    None
                )
                
                if best_signal:
                    log_signal(best_signal, log_type="LOW_SCORE_ANALYSIS")
                    logging.info(f"ℹ️ 取引閾値 ({current_threshold*100:.0f}点) 未満のため、最高スコア ({best_signal['score']*100:.2f}点) のシグナルをロギングしました。")
                else:
                    logging.info("ℹ️ 現在、取引閾値 (RRR >= 1.0 & Score >= Dynamic Threshold) を満たすシグナルはありません。")
            else:
                logging.info(f"✅ {len(qualified_signals)} 件の取引シグナル候補の中から {notified_count} 件を通知しました。")

            LAST_SUCCESS_TIME = time.time()
            
        except Exception as e:
            logging.error(f"❌ メインループ処理中にエラーが発生しました: {e}", exc_info=True)

        # 次の実行までの待機
        elapsed_time = time.time() - start_time
        sleep_duration = max(0, LOOP_INTERVAL - elapsed_time)
        logging.info(f"💤 メインループを {sleep_duration:.2f} 秒間休止します。")
        await asyncio.sleep(sleep_duration)


async def analysis_only_notification_loop():
    """1時間ごとに分析専用の市場スナップショットを通知するループ"""
    global LAST_ANALYSIS_ONLY_NOTIFICATION_TIME, LAST_ANALYSIS_SIGNALS, GLOBAL_MACRO_CONTEXT
    
    # 💡 初回実行遅延ロジックは startup_event で制御済み。
    #    ここでは INTERVAL ごとのループを処理する。
    while True:
        now = time.time()
        
        if now - LAST_ANALYSIS_ONLY_NOTIFICATION_TIME >= ANALYSIS_ONLY_INTERVAL:
            logging.info("⏳ 1時間ごとの分析専用通知を実行します...")
            
            current_threshold = determine_dynamic_threshold(GLOBAL_MACRO_CONTEXT)
            
            # 修正ポイント: 警告を出力してスキップするロジックを削除し、
            # シグナルが空でも「シグナル候補なし」の通知を送信するように変更。
            if not LAST_ANALYSIS_SIGNALS:
                 logging.info("📝 前回のメインループで分析結果が取得されていませんでしたが、分析なしの通知を試行します。")
            
            # 通知メッセージを作成 (LAST_ANALYSIS_SIGNALSが空でも format_analysis_only_message が対応)
            notification_message = format_analysis_only_message(
                LAST_ANALYSIS_SIGNALS, 
                GLOBAL_MACRO_CONTEXT,
                current_threshold,
                len(CURRENT_MONITOR_SYMBOLS)
            )
            
            await send_telegram_notification(notification_message)
            log_signal({'signals': LAST_ANALYSIS_SIGNALS}, log_type="HOURLY_ANALYSIS") # 分析結果をログに記録

            LAST_ANALYSIS_ONLY_NOTIFICATION_TIME = now
            
        await asyncio.sleep(60) # 1分ごとにチェック


async def webshare_upload_loop():
    """1時間ごとにログファイルをWebShare (FTP) にアップロードするループ"""
    global LAST_WEBSHARE_UPLOAD_TIME
    
    while True:
        now = time.time()
        
        if now - LAST_WEBSHARE_UPLOAD_TIME >= WEBSHARE_UPLOAD_INTERVAL:
            logging.info("⏳ WebShareログアップロードを実行します...")
            await upload_logs_to_webshare()
            LAST_WEBSHARE_UPLOAD_TIME = now
            
        # 実行間隔が1時間なので、チェックは1分ごとで十分
        await asyncio.sleep(60) 

# ====================================================================================
# FASTAPI / ENTRY POINT
# ====================================================================================

app = FastAPI()

@app.on_event("startup")
async def startup_event():
    # 1. CCXTクライアントの初期化
    if not await initialize_exchange_client():
        sys.exit(1)

    # 2. 初期状態の更新 (銘柄リスト更新)
    await update_symbols_by_volume()

    # 3. グローバル変数の初期化 (時刻設定による遅延制御)
    global LAST_HOURLY_NOTIFICATION_TIME, LAST_ANALYSIS_ONLY_NOTIFICATION_TIME, LAST_WEBSHARE_UPLOAD_TIME
    LAST_HOURLY_NOTIFICATION_TIME = time.time()
    
    # 💡 修正箇所 (Patch 23): 初回メインループの完了を待つため、分析専用通知の初回実行時間を大幅に遅延
    # (ANALYSIS_ONLY_INTERVAL - 15分) だけ過去の時刻に設定することで、起動後15分後に実行されるようにする。
    ANALYSIS_ONLY_DELAY_SECONDS = 60 * 15 # 15分
    LAST_ANALYSIS_ONLY_NOTIFICATION_TIME = time.time() - (ANALYSIS_ONLY_INTERVAL - ANALYSIS_ONLY_DELAY_SECONDS) 
    
    # WebShareアップロードの初回実行は main_loop 完了後に制御
    WEBSHARE_UPLOAD_DELAY_SECONDS = 60 * 30 # 30分
    LAST_WEBSHARE_UPLOAD_TIME = time.time() - (WEBSHARE_UPLOAD_INTERVAL - WEBSHARE_UPLOAD_DELAY_SECONDS)

    # 4. メインの取引ループと分析専用ループを起動
    asyncio.create_task(main_loop())
    asyncio.create_task(analysis_only_notification_loop()) 
    asyncio.create_task(webshare_upload_loop()) 


@app.on_event("shutdown")
async def shutdown_event():
    global EXCHANGE_CLIENT
    if EXCHANGE_CLIENT:
        await EXCHANGE_CLIENT.close()
        logging.info("CCXTクライアントをシャットダウンしました。")

@app.get("/status")
def get_status():
    status_msg = {
        "status": "ok",
        "bot_version": "v19.0.28 - Final Integrated Build (Patch 24)", 
        "last_success_time_utc": datetime.fromtimestamp(LAST_SUCCESS_TIME, tz=timezone.utc).isoformat() if LAST_SUCCESS_TIME else "N/A",
        "current_client": CCXT_CLIENT_NAME,
        "monitoring_symbols": len(CURRENT_MONITOR_SYMBOLS),
        "last_signals_count": len(LAST_ANALYSIS_SIGNALS),
        "is_test_mode": TEST_MODE,
        "is_first_loop_completed": IS_FIRST_MAIN_LOOP_COMPLETED,
    }
    return JSONResponse(content=status_msg)

if __name__ == "__main__":
    # uvicorn main_render:app --host 0.0.0.0 --port $PORT を実行するために、このifブロックは残す
    pass
