# ====================================================================================
# Apex BOT v19.0.28 - Final Integrated Build (Patch 22: WEBSHARE/FTPログアップロード実装)
#
# 修正ポイント:
# 1. 【機能追加】WEBSHARE_HOST/USER/PASS 環境変数を読み込むよう設定。
# 2. 【機能追加】ローカルログを外部ストレージにアップロードする upload_logs_to_webshare 関数を実装 (FTP想定)。
# 3. 【ロジック変更】main_loop内で、ログアップロードを1時間に1回の頻度で実行するよう制御。
# 4. 【バージョン更新】全てのバージョン情報を Patch 22 に更新。
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
LAST_WEBSHARE_UPLOAD_TIME: float = 0.0 # 💡 WebShareアップロード時刻を追跡

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
        breakdown_list.append(f"  - ✅ MACD/RSIモメンタム加速: <code>+{MACD_CROSS_PENALTY_CONST*100:.1f}</code> 点相当 (ペナルティ回避)")

    # 出来高/OBV確証ボーナス
    volume_bonus = tech_data.get('volume_confirmation_bonus', 0.0)
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
        f"<i>Bot Ver: v19.0.28 - Final Integrated Build (Patch 22)</i>"
    )

    return header + macro_section + signal_section + footer


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
        f"<i>Bot Ver: v19.0.28 - Final Integrated Build (Patch 22)</i>"
    )
    return message


async def send_telegram_notification(message: str) -> bool:
    """Telegramにメッセージを送信する"""
    # 💡 TELEGRAM_BOT_TOKEN は TELEGRAM_TOKEN として読み込まれている
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
        with open(local_file, 'rb') as fp:
            ftp.storbinary(f'STOR {remote_file}', fp)

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

def calculate_liquidity_bonus(symbol: str, current_price: float) -> Tuple[float, float]:
    """流動性（板情報）からボーナスを計算する"""
    global EXCHANGE_CLIENT
    if not EXCHANGE_CLIENT:
        return 0.0, 0.0

    try:
        # 深度情報を取得
        orderbook = asyncio.run(EXCHANGE_CLIENT.fetch_order_book(symbol, limit=20))
        
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
    macd_is_favorable = last['MACDh_12_26_9'] > 0 and last['MACD_12_26_9'] > last['MACDs_12_26_9']
    
    tech_data['macd_cross_valid'] = macd_is_favorable
    
    if not macd_is_favorable:
        score -= MACD_CROSS_PENALTY
        tech_data['macd_penalty_value'] = MACD_CROSS_PENALTY
    else:
        tech_data['macd_penalty_value'] = 0.0

    # 5. ストキャスティクスフィルター (Stochastics)
    stoch_k = last['STOCHk_14_3_3']
    stoch_d = last['STOCHd_14_3_3']
    stoch_filter_penalty = 0.0
    if stoch_k > 80 or stoch_d > 80:
        stoch_filter_penalty = 0.05
        score -= stoch_filter_penalty
        
    tech_data['stoch_filter_penalty'] = stoch_filter_penalty
    tech_data['stoch_filter_penalty_value'] = stoch_filter_penalty


    # 6. 出来高・構造・ボラティリティボーナス
    # OBVボーナス (出来高トレンド)
    obv_momentum_bonus = 0.0
    if last['OBV'] > prev_last['OBV']:
        obv_momentum_bonus = OBV_MOMENTUM_BONUS
        score += obv_momentum_bonus
    tech_data['obv_momentum_bonus_value'] = obv_momentum_bonus

    # 価格構造/ピボット支持ボーナス
    structural_pivot_bonus = 0.0
    if current_price > df.iloc[-2]['close'] and current_price > df.iloc[-3]['close']:
        structural_pivot_bonus = STRUCTURAL_PIVOT_BONUS
        score += structural_pivot_bonus
    tech_data['structural_pivot_bonus'] = structural_pivot_bonus

    # ボラティリティ評価ペナルティ/スコア
    vol_score, bb_width_ratio = calculate_volatility_score(df, current_price)
    if vol_score < 1.0:
        score += (vol_score - 1.0) * 0.1 # 最大-10%のペナルティ

    # 7. マクロ要因の適用
    # FGI (恐怖・貪欲指数)
    fgi_proxy = macro_context.get('fgi_proxy', 0.0)
    score += fgi_proxy
    tech_data['sentiment_fgi_proxy_bonus'] = fgi_proxy

    # 為替マクロ (常に0.0)
    forex_bonus = macro_context.get('forex_bonus', 0.0)
    score += forex_bonus
    tech_data['forex_bonus_value'] = forex_bonus
    
    # 8. リスクリワード比率 (RRR)とTP/SLの決定
    # 簡易的なSL/TP計算
    atr_val = ta.atr(df['high'], df['low'], df['close'], length=14).iloc[-1]
    stop_loss_price = current_price - (atr_val * 2.5) # ATRの2.5倍をリスク許容度とする
    risk_amount = current_price - stop_loss_price
    take_profit_price = current_price + (risk_amount * 2.0) # 簡易的にRRR=2.0
    
    if stop_loss_price < current_price:
        rr_ratio = (take_profit_price - current_price) / (current_price - stop_loss_price)
    else:
        rr_ratio = 0.0

    # 9. スコアの正規化と丸め
    score = max(0.0, min(1.0, score))

    # 10. シグナル結果の作成
    signal_result = {
        'symbol': df.name,
        'timeframe': timeframe,
        'score': score,
        'entry_price': current_price,
        'stop_loss': stop_loss_price,
        'take_profit': take_profit_price,
        'rr_ratio': rr_ratio,
        'tech_data': tech_data
    }
    
    # RRRが低い場合は最終スコアをペナルティする
    if rr_ratio < 1.0:
        signal_result['score'] *= 0.8 

    return signal_result


async def analyze_all_symbols(macro_context: Dict) -> List[Dict]:
    """すべての銘柄と時間足で分析を実行する"""
    tasks = []
    
    for symbol in CURRENT_MONITOR_SYMBOLS:
        for timeframe in TARGET_TIMEFRAMES:
            limit = REQUIRED_OHLCV_LIMITS[timeframe]
            # データ取得と分析を並行して実行
            tasks.append(
                analyze_and_score(symbol, timeframe, limit, macro_context)
            )
            
    all_signals = [result for result in await asyncio.gather(*tasks) if result is not None]
    
    # 総合スコアでソート
    all_signals.sort(key=lambda s: s['score'], reverse=True)
    
    return all_signals

async def analyze_and_score(symbol: str, timeframe: str, limit: int, macro_context: Dict) -> Optional[Dict]:
    """単一銘柄・時間足のデータ取得からスコアリングまでを実行する"""
    try:
        df = await fetch_ohlcv_with_fallback(symbol, timeframe, limit)
        if df is None or df.empty:
            return None
            
        df.name = symbol # DataFrameに銘柄名を付与
        
        # 1. テクニカル分析と基本スコア
        signal = analyze_single_timeframe(df, timeframe, macro_context)
        if signal is None:
            return None

        # 2. 流動性ボーナス
        liquidity_bonus, liquidity_ratio = await asyncio.to_thread(
            calculate_liquidity_bonus, symbol, signal['entry_price']
        )
        
        signal['score'] += liquidity_bonus
        signal['tech_data']['liquidity_bonus_value'] = liquidity_bonus
        signal['tech_data']['liquidity_ratio'] = liquidity_ratio

        # 3. 最終スコアの正規化
        signal['score'] = max(0.0, min(1.0, signal['score']))
        
        return signal
        
    except Exception as e:
        return None

# ====================================================================================
# MAIN BOT LOOPS
# ====================================================================================

async def analysis_only_notification_loop():
    """1時間に一度、最高スコア銘柄を通知する分析専用ループ"""
    global LAST_ANALYSIS_ONLY_NOTIFICATION_TIME, LAST_ANALYSIS_SIGNALS

    if LAST_ANALYSIS_ONLY_NOTIFICATION_TIME == 0.0:
        LAST_ANALYSIS_ONLY_NOTIFICATION_TIME = time.time() - (ANALYSIS_ONLY_INTERVAL * 2)

    while True:
        await asyncio.sleep(5) # 5秒間隔でチェック

        time_since_last_notification = time.time() - LAST_ANALYSIS_ONLY_NOTIFICATION_TIME
        
        # 1. 1時間に一度の通知タイミングか確認
        if time_since_last_notification >= ANALYSIS_ONLY_INTERVAL:
            logging.info("⏳ 1時間ごとの分析専用通知を実行します...")
            
            # 2. 最終分析結果の取得とマクロコンテキストの取得
            if not LAST_ANALYSIS_SIGNALS:
                 logging.warning("⚠️ 前回のメインループで分析結果が取得されていません。スキップします。")
                 LAST_ANALYSIS_ONLY_NOTIFICATION_TIME = time.time()
                 continue
                 
            macro_context = await get_crypto_macro_context()
            
            # 市場環境に応じて動的閾値を計算 (Hourly Loopでも必要)
            fgi_proxy = macro_context.get('fgi_proxy', 0.0)
            if fgi_proxy < FGI_SLUMP_THRESHOLD:
                effective_threshold = SIGNAL_THRESHOLD_SLUMP
            elif fgi_proxy > FGI_ACTIVE_THRESHOLD:
                effective_threshold = SIGNAL_THRESHOLD_ACTIVE
            else:
                effective_threshold = SIGNAL_THRESHOLD_NORMAL
            
            # 3. 通知メッセージの作成
            message = format_analysis_only_message(LAST_ANALYSIS_SIGNALS, macro_context, effective_threshold, len(CURRENT_MONITOR_SYMBOLS))
            
            # 4. Telegram通知とログ記録
            await send_telegram_notification(message)
            
            # 最高スコア銘柄を必ずログに残す
            if LAST_ANALYSIS_SIGNALS:
                top_signal = LAST_ANALYSIS_SIGNALS[0].copy()
                top_signal['tech_data']['macro_context'] = macro_context # マクロコンテキストもログに含める
                log_signal(top_signal, log_type="HOURLY_ANALYSIS")
                
            # 5. 時間のリセット
            LAST_ANALYSIS_ONLY_NOTIFICATION_TIME = time.time()

        
async def main_loop():
    """メインの取引ロジックと分析処理を実行する"""
    global LAST_SUCCESS_TIME, LAST_SIGNAL_TIME, LAST_ANALYSIS_SIGNALS, LAST_WEBSHARE_UPLOAD_TIME

    while True:
        start_time = time.time()
        
        try:
            # 1. 銘柄リストとマクロコンテキストの更新
            if start_time - LAST_SUCCESS_TIME > 60 * 60 * 4 or not CURRENT_MONITOR_SYMBOLS or len(CURRENT_MONITOR_SYMBOLS) < 30: # 4時間に一度、または30銘柄未満の場合に更新
                await update_symbols_by_volume()
                
            macro_context = await get_crypto_macro_context()
            
            # 市場環境に応じて動的閾値を計算
            fgi_proxy = macro_context.get('fgi_proxy', 0.0)
            
            if fgi_proxy < FGI_SLUMP_THRESHOLD:
                effective_threshold = SIGNAL_THRESHOLD_SLUMP
                market_condition = "低迷 (Slump)"
            elif fgi_proxy > FGI_ACTIVE_THRESHOLD:
                effective_threshold = SIGNAL_THRESHOLD_ACTIVE
                market_condition = "活発 (Active)"
            else:
                effective_threshold = SIGNAL_THRESHOLD_NORMAL
                market_condition = "通常 (Normal)"
                
            logging.info(f"📈 現在の市場環境: {market_condition} (FGI Proxy: {fgi_proxy:.3f})。取引閾値: {effective_threshold*100:.2f}点に設定されました。")
            
            # 2. 全銘柄・全時間足で分析を実行
            if len(CURRENT_MONITOR_SYMBOLS) < 30:
                logging.warning(f"⚠️ 銘柄数が30未満 ({len(CURRENT_MONITOR_SYMBOLS)}) のため、分析結果の信頼度が低下する可能性があります。")

            all_signals = await analyze_all_symbols(macro_context)
            LAST_ANALYSIS_SIGNALS = all_signals # グローバル変数に保存 (hourly loop用)
            
            if not all_signals:
                logging.info("ℹ️ 今回の分析で有効なシグナルは見つかりませんでした。")
                LAST_SUCCESS_TIME = time.time()
                await asyncio.sleep(LOOP_INTERVAL - (time.time() - start_time))
                continue

            # 3. 取引シグナルのフィルタリング
            trade_signals: List[Dict] = []
            
            for signal in all_signals:
                symbol = signal['symbol']
                score = signal['score']
                rr_ratio = signal['rr_ratio']
                
                # スコアが動的閾値以上
                if score >= effective_threshold:
                    # RRRが最低でも1.0以上
                    if rr_ratio >= 1.0:
                        # クールダウン期間をチェック
                        last_time = LAST_SIGNAL_TIME.get(symbol, 0.0)
                        if (time.time() - last_time) >= TRADE_SIGNAL_COOLDOWN:
                            trade_signals.append(signal)

            # 4. シグナルの処理 (通知と取引)
            if trade_signals:
                logging.info(f"🚨 {len(trade_signals)} 件の取引シグナルが見つかりました。")
                
                for signal in trade_signals:
                    symbol = signal['symbol']
                    
                    # 4.1. Telegram通知
                    context = "TEST MODE" if TEST_MODE else "LIVE TRADE"
                    message = format_telegram_message(signal, context, effective_threshold)
                    await send_telegram_notification(message)
                    
                    # 4.2. ログ記録
                    log_signal(signal, log_type="TRADE_SIGNAL")
                    
                    # 4.3. 取引実行 (ここでは通知とログのみ)
                    if not TEST_MODE:
                        logging.warning(f"⚠️ LIVE MODE: {symbol} の現物購入実行ロジックは未実装です。")
                        pass 
                        
                    # 4.4. クールダウン時間更新
                    LAST_SIGNAL_TIME[symbol] = time.time()
            else:
                logging.info(f"ℹ️ 今回の分析で動的閾値 ({effective_threshold*100:.2f}点) と RRR >= 1.0 を満たすシグナルは見つかりませんでした。")
                
            # 5. WebShareログアップロードの実行 (1時間に1回)
            if time.time() - LAST_WEBSHARE_UPLOAD_TIME >= WEBSHARE_UPLOAD_INTERVAL:
                await upload_logs_to_webshare()
                LAST_WEBSHARE_UPLOAD_TIME = time.time()
                
            # 6. 成功時間更新
            LAST_SUCCESS_TIME = time.time()
            
        except Exception as e:
            logging.critical(f"❌ メインループで致命的なエラーが発生しました: {e}", exc_info=True)
            
        # 7. 次のループまで待機
        elapsed_time = time.time() - start_time
        sleep_time = max(0, LOOP_INTERVAL - elapsed_time)
        logging.info(f"♻️ 次のループまで {sleep_time:.1f} 秒待機します。")
        await asyncio.sleep(sleep_time)


# ====================================================================================
# FASTAPI / ENTRY POINT
# ====================================================================================

app = FastAPI(title="Apex Trading Bot API", version="v19.0.28 - Patch 22: WEBSHARE実装")

@app.on_event("startup")
async def startup_event():
    # 1. CCXTクライアントの初期化
    if not await initialize_exchange_client():
        sys.exit(1)

    # 2. 初期状態の更新 (銘柄リスト更新)
    await update_symbols_by_volume()

    # 3. グローバル変数の初期化
    global LAST_HOURLY_NOTIFICATION_TIME, LAST_ANALYSIS_ONLY_NOTIFICATION_TIME, LAST_WEBSHARE_UPLOAD_TIME
    LAST_HOURLY_NOTIFICATION_TIME = time.time()
    
    # 分析専用通知の初回実行は main_loop 完了後に analysis_only_notification_loop 内で制御
    LAST_ANALYSIS_ONLY_NOTIFICATION_TIME = time.time() - (ANALYSIS_ONLY_INTERVAL * 2)
    # WebShareアップロードの初回実行は main_loop 完了後に制御
    LAST_WEBSHARE_UPLOAD_TIME = time.time() - (WEBSHARE_UPLOAD_INTERVAL * 2)

    # 4. メインの取引ループと分析専用ループを起動
    asyncio.create_task(main_loop())
    asyncio.create_task(analysis_only_notification_loop())

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
        "bot_version": "v19.0.28 - Final Integrated Build (Patch 22)",
        "test_mode": TEST_MODE,
        "base_signal_threshold": SIGNAL_THRESHOLD_NORMAL, # ベースラインの閾値を表示
        "last_success_time_jst": datetime.fromtimestamp(LAST_SUCCESS_TIME, tz=JST).strftime("%Y-%m-%d %H:%M:%S") if LAST_SUCCESS_TIME else "N/A",
        "current_client": CCXT_CLIENT_NAME,
        "monitoring_symbols_count": len(CURRENT_MONITOR_SYMBOLS),
        "last_signals_count": len(LAST_ANALYSIS_SIGNALS),
        "loop_interval_sec": LOOP_INTERVAL,
        "analysis_interval_sec": ANALYSIS_ONLY_INTERVAL,
        "next_analysis_notification_in_sec": max(0, ANALYSIS_ONLY_INTERVAL - (time.time() - LAST_ANALYSIS_ONLY_NOTIFICATION_TIME)),
        "next_webshare_upload_in_sec": max(0, WEBSHARE_UPLOAD_INTERVAL - (time.time() - LAST_WEBSHARE_UPLOAD_TIME))
    }
    return JSONResponse(content=status_msg)
