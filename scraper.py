#!/usr/bin/env python3
"""
カーセンサー在庫スクレイピングスクリプト (Playwright版)
オートボディ菅原の在庫情報を取得しJSON出力
"""

import json
import time
import logging
import re
import os
import subprocess
from typing import List, Dict, Optional
from urllib.parse import urljoin
from datetime import datetime

from playwright.sync_api import sync_playwright, Page, TimeoutError as PlaywrightTimeout
from dotenv import load_dotenv

# .envファイルから環境変数を読み込み
load_dotenv()

# ロギング設定
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# 定数（環境変数から読み込み、デフォルト値を設定）
BASE_URL = os.getenv('BASE_URL', 'https://www.carsensor.net')
SHOP_URL = os.getenv('SHOP_URL', 'https://www.carsensor.net/shop/miyagi/326411001/stocklist/')
OUTPUT_FILE = os.getenv('OUTPUT_FILE', 'data/carsensor_inventory.json')
REQUEST_DELAY = int(os.getenv('REQUEST_DELAY', '2'))  # リクエスト間隔（秒）
PAGE_TIMEOUT = int(os.getenv('PAGE_TIMEOUT', '30000'))  # ページ読み込みタイムアウト（ミリ秒）
SHOP_MAX_RETRIES = int(os.getenv('SHOP_MAX_RETRIES', '3'))  # 店舗ページ取得の最大試行回数
DETAIL_MAX_RETRIES = int(os.getenv('DETAIL_MAX_RETRIES', '3'))  # 詳細ページ取得の最大試行回数
RETRY_DELAY = int(os.getenv('RETRY_DELAY', '5'))  # リトライ間隔（秒）
SHOP_WAIT_UNTIL = os.getenv('SHOP_WAIT_UNTIL', 'domcontentloaded')
SHOP_LINK_STABILITY_WAIT = int(os.getenv('SHOP_LINK_STABILITY_WAIT', '2000'))  # リンク数の安定待ち（ミリ秒）
SHOP_LINK_STABILITY_TIMEOUT = int(os.getenv('SHOP_LINK_STABILITY_TIMEOUT', '10000'))  # 安定待ちの上限（ミリ秒）
DETAIL_WAIT_UNTIL = os.getenv('DETAIL_WAIT_UNTIL', 'domcontentloaded')
CAR_LINK_SELECTOR = 'a[href*="/usedcar/detail/"]'

# User-Agent
USER_AGENT = os.getenv('USER_AGENT', 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')


def get_car_link_urls(page: Page) -> List[str]:
    """在庫リストページから重複を除いた車両詳細URLを取得する。"""
    links = []
    car_items = page.query_selector_all(CAR_LINK_SELECTOR)

    for item in car_items:
        href = item.get_attribute('href')
        if href:
            full_url = urljoin(BASE_URL, href)
            if full_url not in links:
                links.append(full_url)

    return links


def extract_car_links(page: Page) -> List[str]:
    """
    在庫リストページから各車両の詳細ページURLを抽出

    Args:
        page: Playwrightページオブジェクト

    Returns:
        車両詳細ページURLのリスト
    """
    try:
        links = get_car_link_urls(page)
        logger.info(f"Found {len(links)} car links")
        return links

    except Exception as e:
        logger.error(f"Failed to extract car links: {e}")
        return []


def extract_table_value(page: Page, label: str) -> str:
    """
    テーブルから特定のラベルに対応する値を抽出

    Args:
        page: Playwrightページオブジェクト
        label: 検索するラベル（例: "年式", "走行距離"）

    Returns:
        抽出された値（見つからない場合は空文字列）
    """
    try:
        # すべてのthタグを取得
        th_elements = page.query_selector_all('th.defaultTable__head')

        for th in th_elements:
            th_text = th.inner_text()
            if label in th_text:
                # 同じ行のtd要素を取得
                td = th.evaluate('el => el.parentElement?.querySelector("td.defaultTable__description")')
                if td:
                    # tdのテキストを取得
                    value = th.evaluate('el => el.parentElement?.querySelector("td.defaultTable__description")?.innerText')
                    if value:
                        return value.strip()
        return ""

    except Exception as e:
        logger.debug(f"Failed to extract table value for '{label}': {e}")
        return ""


def clean_inspection_text(inspection_text: str) -> str:
    """
    車検情報をクリーンアップ
    長い説明文から「車検整備付」などの重要情報のみを抽出

    Args:
        inspection_text: 元の車検情報テキスト

    Returns:
        クリーンアップされた車検情報
    """
    if not inspection_text:
        return ""
    
    # 車検整備付が含まれる場合はそれを返す
    if "車検整備付" in inspection_text:
        return "車検整備付"
    
    # 通常の車検年月の場合（例: "2026(R08)年10月"）
    # 複数行の説明文でない場合はそのまま返す
    if "\n" not in inspection_text or len(inspection_text) < 50:
        return inspection_text.strip()
    
    # 長い説明文の場合、最初の行のみを返す
    return inspection_text.split('\n')[0].strip()


def extract_car_details(page: Page, url: str) -> Optional[Dict[str, str]]:
    """
    車両詳細ページから情報を抽出

    Args:
        page: Playwrightページオブジェクト
        url: 車両詳細ページのURL

    Returns:
        車両情報の辞書、失敗時はNone
    """
    try:
        logger.info(f"Fetching: {url}")

        # ページにアクセス
        page.goto(url, wait_until=DETAIL_WAIT_UNTIL, timeout=PAGE_TIMEOUT)

        # ページが完全に読み込まれるまで少し待機
        page.wait_for_timeout(1000)

        # 車名を抽出
        name = "不明"
        description = ""
        try:
            h1_element = page.query_selector('h1')
            if h1_element:
                full_name = h1_element.inner_text().strip()
                # \nで分割して、最初の部分をname、残りをdescriptionに
                if '\n' in full_name:
                    parts = full_name.split('\n', 1)
                    name = parts[0].strip()
                    description = parts[1].strip() if len(parts) > 1 else ""
                else:
                    name = full_name
        except:
            pass

        # 本体価格を抽出
        price = ""
        try:
            price_element = page.query_selector('p.basePrice__price')
            if price_element:
                price_text = price_element.inner_text().strip()
                # 価格を整形（改行を削除して統一）
                price = re.sub(r'\s+', '', price_text)
        except:
            pass

        # 支払総額を抽出
        total_price = ""
        try:
            # 支払総額の要素を探す
            total_price_element = page.query_selector('p.totalPrice__price')
            if total_price_element:
                total_price_text = total_price_element.inner_text().strip()
                # 価格を整形（改行を削除して統一）
                total_price = re.sub(r'\s+', '', total_price_text)
        except:
            pass

        # テーブル情報を抽出
        year = extract_table_value(page, '年式')
        mileage = extract_table_value(page, '走行距離')
        engine_size = extract_table_value(page, '排気量')
        inspection_raw = extract_table_value(page, '車検')
        inspection = clean_inspection_text(inspection_raw)
        repair_history = extract_table_value(page, '修復歴')

        # メイン画像URLを抽出
        image_url = ""
        try:
            img_element = page.query_selector('img[class*="main"]')
            if img_element:
                image_url = img_element.get_attribute('src') or ''
        except:
            pass

        car_data = {
            "name": name,
            "description": description,
            "year": year,
            "mileage": mileage,
            "engine_size": engine_size,
            "inspection": inspection,
            "repair_history": repair_history,
            "price": price,
            "total_price": total_price,
            "image_url": image_url,
            "detail_link": url
        }

        logger.info(f"Extracted: {name} - 本体価格: {price}, 支払総額: {total_price}")
        return car_data

    except PlaywrightTimeout:
        logger.error(f"Timeout loading page: {url}")
        return None
    except Exception as e:
        logger.error(f"Failed to extract details from {url}: {e}")
        return None


def extract_car_details_with_retries(page: Page, url: str) -> Optional[Dict[str, str]]:
    """
    車両詳細ページから情報を取得。失敗時は一定回数リトライする。

    Args:
        page: Playwrightページオブジェクト
        url: 車両詳細ページのURL

    Returns:
        車両情報の辞書、全試行失敗時はNone
    """
    max_attempts = max(1, DETAIL_MAX_RETRIES)

    for attempt in range(1, max_attempts + 1):
        car_data = extract_car_details(page, url)
        if car_data:
            if attempt > 1:
                logger.info(f"Successfully fetched after retry {attempt}/{max_attempts}: {url}")
            return car_data

        if attempt < max_attempts:
            delay = RETRY_DELAY * attempt
            logger.warning(
                f"Retrying detail page {attempt + 1}/{max_attempts} in {delay} seconds: {url}"
            )
            time.sleep(delay)

    logger.error(f"Failed to fetch detail page after {max_attempts} attempts: {url}")
    return None


def wait_for_stable_car_links(page: Page) -> bool:
    """
    車両リンク数が一定時間変化しないことを確認する。

    店舗ページは初期表示後に車両リンクが追加される場合があるため、
    最初のリンクが見えただけでは処理を開始しない。

    Returns:
        リンク数が安定した場合True、上限時間内に安定しない場合False
    """
    stability_wait = max(0, SHOP_LINK_STABILITY_WAIT)
    stability_timeout = max(stability_wait, SHOP_LINK_STABILITY_TIMEOUT)
    deadline = time.monotonic() + (stability_timeout / 1000)
    previous_links = set(get_car_link_urls(page))

    if not previous_links:
        logger.warning("Shop page has no car links after selector became available")
        return False

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            logger.warning(
                f"Car link count did not stabilize within {stability_timeout} ms"
            )
            return False

        if stability_wait:
            wait_ms = min(stability_wait, max(1, int(remaining * 1000)))
            page.wait_for_timeout(wait_ms)

        current_links = set(get_car_link_urls(page))
        if current_links == previous_links:
            logger.info(f"Car link set stabilized at {len(current_links)} unique links")
            return True

        logger.info(
            f"Car link set changed from {len(previous_links)} to {len(current_links)} unique links; "
            "continuing to wait"
        )
        previous_links = current_links


def load_shop_page_with_retries(page: Page) -> bool:
    """
    店舗在庫ページを読み込み、車両リンクが現れるまで最大試行回数内で再試行する。

    Returns:
        読み込み成功時True、全試行失敗時False
    """
    max_attempts = max(1, SHOP_MAX_RETRIES)

    for attempt in range(1, max_attempts + 1):
        try:
            logger.info(
                f"Fetching shop page (attempt {attempt}/{max_attempts}): {SHOP_URL}"
            )
            page.goto(SHOP_URL, wait_until=SHOP_WAIT_UNTIL, timeout=PAGE_TIMEOUT)
            page.wait_for_selector(CAR_LINK_SELECTOR, state='attached', timeout=PAGE_TIMEOUT)
            if not wait_for_stable_car_links(page):
                raise RuntimeError("Car link count did not stabilize")

            if attempt > 1:
                logger.info(f"Successfully loaded shop page after retry {attempt}/{max_attempts}")
            return True
        except PlaywrightTimeout:
            logger.warning(f"Shop page attempt {attempt}/{max_attempts} timed out")
        except Exception as e:
            logger.warning(f"Shop page attempt {attempt}/{max_attempts} failed: {e}")

        if attempt < max_attempts:
            delay = RETRY_DELAY * attempt
            logger.warning(
                f"Retrying shop page {attempt + 1}/{max_attempts} in {delay} seconds"
            )
            time.sleep(delay)

    logger.error(f"Failed to load shop page after {max_attempts} attempts: {SHOP_URL}")
    return False


def scrape_inventory() -> List[Dict[str, str]]:
    """
    在庫リスト全体をスクレイピング

    Returns:
        車両情報のリスト
    """
    logger.info("Starting scraping process...")
    inventory = []
    failed_links = []

    with sync_playwright() as p:
        # ブラウザを起動
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=USER_AGENT)
        page = context.new_page()

        try:
            # 在庫リストページにアクセス
            if not load_shop_page_with_retries(page):
                return []

            # 車両リンクを抽出
            car_links = extract_car_links(page)
            if not car_links:
                logger.warning("No car links found")
                return []

            # 各車両の詳細を取得
            for i, link in enumerate(car_links, 1):
                logger.info(f"Processing car {i}/{len(car_links)}")

                car_data = extract_car_details_with_retries(page, link)
                if car_data:
                    inventory.append(car_data)
                else:
                    failed_links.append(link)

                # サーバーに負荷をかけないよう待機
                if i < len(car_links):
                    time.sleep(REQUEST_DELAY)

            if failed_links or len(inventory) != len(car_links):
                logger.error(
                    "Incomplete scrape: expected %s cars but scraped %s. "
                    "Skipping JSON save and Git push. Failed links: %s",
                    len(car_links),
                    len(inventory),
                    ", ".join(failed_links) if failed_links else "(unknown)",
                )
                return []

            logger.info(f"Successfully scraped {len(inventory)} cars")

        except PlaywrightTimeout:
            logger.error("Timeout loading shop page")
        except Exception as e:
            logger.error(f"Error during scraping: {e}")
        finally:
            browser.close()

    return inventory


def save_to_json(data: List[Dict[str, str]], filepath: str) -> bool:
    """
    データをJSON形式で保存

    Args:
        data: 保存するデータ
        filepath: 保存先ファイルパス

    Returns:
        成功時True、失敗時False
    """
    try:
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.info(f"Data saved to {filepath}")
        return True
    except Exception as e:
        logger.error(f"Failed to save data: {e}")
        return False


def git_commit_and_push() -> bool:
    """
    データファイルをGitにコミット＆プッシュ

    環境変数:
        GITHUB_TOKEN: GitHub Personal Access Token (必須)
        GITHUB_USER_NAME: Gitユーザー名 (オプション)
        GITHUB_USER_EMAIL: Gitメールアドレス (オプション)

    Returns:
        成功時True、失敗時False
    """
    try:
        # GitHub Tokenの確認
        github_token = os.getenv('GITHUB_TOKEN')
        if not github_token:
            logger.warning("GITHUB_TOKEN not set. Skipping git push.")
            return False

        # Git設定
        git_user = os.getenv('GITHUB_USER_NAME', 'Carsensor Scraper')
        git_email = os.getenv('GITHUB_USER_EMAIL', 'scraper@example.com')

        logger.info("Configuring Git...")
        subprocess.run(['git', 'config', 'user.name', git_user], check=True)
        subprocess.run(['git', 'config', 'user.email', git_email], check=True)

        # ファイルを追加
        logger.info(f"Adding {OUTPUT_FILE}...")
        subprocess.run(['git', 'add', OUTPUT_FILE], check=True)

        # 在庫データの変更だけを確認
        result = subprocess.run(
            ['git', 'diff', '--cached', '--quiet', '--', OUTPUT_FILE]
        )

        if result.returncode == 0:
            logger.info("No inventory changes to commit.")
            return True
        if result.returncode != 1:
            result.check_returncode()

        # コミット
        commit_message = f"Update inventory data - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        logger.info(f"Committing: {commit_message}")
        subprocess.run(['git', 'commit', '-m', commit_message], check=True)

        # プッシュ（認証情報をURLに埋め込む）
        logger.info("Pushing to GitHub...")
        # リモートURLを取得
        result = subprocess.run(
            ['git', 'config', '--get', 'remote.origin.url'],
            capture_output=True,
            text=True,
            check=True
        )
        remote_url = result.stdout.strip()

        # HTTPSの場合、トークンを埋め込む
        if remote_url.startswith('https://'):
            # https://github.com/user/repo.git -> https://token@github.com/user/repo.git
            auth_url = remote_url.replace('https://', f'https://{github_token}@')
            subprocess.run(['git', 'push', auth_url, 'HEAD'], check=True)
        else:
            # SSH URLの場合はそのままプッシュ
            subprocess.run(['git', 'push'], check=True)

        logger.info("Successfully pushed to GitHub!")
        return True

    except subprocess.CalledProcessError as e:
        logger.error(f"Git command failed: {e}")
        return False
    except Exception as e:
        logger.error(f"Failed to push to GitHub: {e}")
        return False


def main():
    """メイン処理"""
    start_time = time.time()

    # スクレイピング実行
    inventory = scrape_inventory()

    # JSON保存
    if inventory:
        if not save_to_json(inventory, OUTPUT_FILE):
            logger.error("Failed to save JSON file")
            return 1

        # GitHubにプッシュ（環境変数で有効化した場合のみ）
        auto_push = os.getenv('AUTO_GIT_PUSH', 'false').lower() in ('true', '1', 'yes')
        if auto_push:
            logger.info("AUTO_GIT_PUSH is enabled. Pushing to GitHub...")
            git_commit_and_push()
        else:
            logger.info("Data saved successfully. Run 'git add/commit/push' manually if needed.")

        logger.info(f"Scraping completed in {time.time() - start_time:.2f} seconds")
    else:
        logger.error("No data to save")
        return 1

    return 0


if __name__ == "__main__":
    exit(main())
