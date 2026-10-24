"""Human login on the official website; only Boosty cookies go to loopback admin."""
import argparse
import sys
import time

from manage import admin_origin, admin_request


def main():
    parser = argparse.ArgumentParser(description="Sign in to Boosty in a separate visible browser")
    parser.add_argument("--admin-url", default="http://127.0.0.1:19117")
    parser.add_argument("--key-file", required=True)
    parser.add_argument("--browser", choices=("chrome", "msedge", "chromium"), default="msedge")
    args = parser.parse_args()
    admin_origin(args.admin_url)
    admin_request(args.admin_url, args.key_file, "status")
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=False, channel=None if args.browser == "chromium" else args.browser,
            args=["--renderer-process-limit=2", "--disable-background-networking"])
        try:
            context = browser.new_context()
            page = context.new_page()
            page.goto("https://boosty.to/", wait_until="domcontentloaded")
            print("Войдите на официальном сайте Boosty в открытом окне. Пароль и код не вводите в терминал.", flush=True)
            deadline = time.monotonic() + 600
            while time.monotonic() < deadline:
                cookies = [c for c in context.cookies("https://boosty.to/") if c["name"] in ("auth", "_clientId") and c["domain"].lstrip(".") == "boosty.to"]
                if {c["name"] for c in cookies} == {"auth", "_clientId"}:
                    admin_request(args.admin_url, args.key_file, "session", {"cookies": cookies})
                    print("Аккаунт подключён к вашему серверу. Пароль не сохранялся; файл cookies не создавался.")
                    return
                page.wait_for_timeout(1500)
            raise TimeoutError("Login did not finish")
        finally:
            browser.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print("Вход не завершён: " + type(error).__name__ + ". Проверьте браузер и локальное подключение к серверу.", file=sys.stderr)
        sys.exit(1)
