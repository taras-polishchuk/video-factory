// Real-browser regression walk for the customer path (UI U1).
//
// Run by tests/test_u1_ui_browser.py against a live API + `vite dev`:
//   node web/e2e/customer-path.mjs <baseUrl> <outJson>
//
// Covers the defects the SvelteKit app shipped with:
//   A-1/A-4  layout hijack: /credentials unreachable until a key exists
//   A-2      POST /login 500 (default + named action mix)
//   A-5      controls without accessible names
//   A-6      no navigation at <= 880px
//   plus     CSRF header on browser mutations (bible approve)
//
// Uses only the `playwright` library already in web/package.json (no
// test runner). Exit code 0 = every step passed.

import { chromium } from "playwright";
import fs from "node:fs";

const [BASE, OUT] = process.argv.slice(2);
const steps = [];
const rec = (name, ok, note = "") => {
	steps.push({ name, ok, note });
};
const rnd = Math.random().toString(16).slice(2, 8);
const EMAIL = `e2e-${rnd}@example.com`;
const PASSWORD = "Sup3r-secret-pass!";

async function step(name, fn) {
	try {
		const r = await fn();
		rec(name, r?.ok !== false, r?.note || "");
	} catch (e) {
		rec(name, false, String(e.message).split("\n")[0].slice(0, 240));
	}
}

// Wait for the JS bundle to hydrate: typed values / clicks made before
// hydration can be lost (vite dev may also reload once after optimising).
async function go(page, url) {
	await page.goto(url);
	await page.waitForLoadState("networkidle").catch(() => {});
	await page.waitForTimeout(250);
}

async function unlabeled(page) {
	return page.evaluate(() => {
		const out = [];
		for (const el of document.querySelectorAll("input, select, textarea")) {
			if (el.type === "hidden" || getComputedStyle(el).display === "none") continue;
			const named =
				(el.labels && el.labels.length > 0) ||
				el.getAttribute("aria-label") ||
				el.getAttribute("aria-labelledby");
			if (!named) out.push(el.name || el.id || el.type || el.tagName);
		}
		return out;
	});
}

const browser = await chromium.launch({ headless: true });
try {
	// ---------------- desktop ----------------
	const ctx = await browser.newContext({ viewport: { width: 1280, height: 800 } });
	const page = await ctx.newPage();
	page.setDefaultTimeout(10000);
	const posts = [];
	page.on("response", (r) => {
		if (r.request().method() === "POST" && r.url().includes("/login")) posts.push(r.status());
	});

	await step("signup form lands on overview with onboarding", async () => {
		await go(page, BASE + "/signup");
		await page.fill("input[name=email]", EMAIL);
		await page.fill("input[name=password]", PASSWORD);
		await page.fill("input[name=workspace_name]", "E2E Co " + rnd);
		await page.click("button[type=submit]");
		await page.waitForURL((u) => u.pathname === "/");
		const cta = await page.getByRole("link", { name: /add provider key/i }).count();
		return { ok: cta > 0, note: `cta links=${cta}` };
	});

	await step("A-1/A-4: CTA opens the credentials page (not the onboarding)", async () => {
		await page.getByRole("link", { name: /add provider key/i }).first().click();
		await page.waitForURL((u) => u.pathname === "/credentials");
		const h1 = await page.locator("h1").first().innerText();
		// the provider list appears once the credentials fetch resolves
		await page.getByText("HeyGen (direct REST)").first().waitFor();
		return { ok: /api keys/i.test(h1), note: `h1=${h1}` };
	});

	for (const [path, re] of [
		["/bible", /video bible/i],
		["/create", /create a video/i],
		["/jobs", /jobs/i],
		["/assets", /assets/i],
		["/integrations", /integrations/i],
	]) {
		await step(`A-1: ${path} renders its own page with no provider key`, async () => {
			await go(page, BASE + path);
			const h1 = await page.locator("h1").first().innerText();
			return { ok: re.test(h1), note: `h1=${h1}` };
		});
	}

	await step("A-5: every control on /create, /bible, /credentials has a name", async () => {
		const bad = [];
		for (const path of ["/create", "/bible", "/credentials"]) {
			await go(page, BASE + path);
			if (path === "/credentials") await page.getByRole("button", { name: /add secret/i }).first().click();
			for (const n of await unlabeled(page)) bad.push(`${path}:${n}`);
		}
		return { ok: bad.length === 0, note: bad.join(", ") };
	});

	await step("browser mutations carry the CSRF token (bible save + approve)", async () => {
		await go(page, BASE + "/bible");
		await page.locator("#bible-audience").fill("home baristas 25-45");
		await page.getByRole("button", { name: /save draft/i }).click();
		await page.getByRole("button", { name: /approve v1/i }).click();
		await page.locator(".version .vf-pill", { hasText: /^\s*approved\s*$/ }).first().waitFor();
		return { ok: (await page.getByRole("button", { name: /approve v1/i }).count()) === 0 };
	});

	await step("save a fake provider key through the credentials UI", async () => {
		await go(page, BASE + "/credentials");
		await page.getByRole("button", { name: /add secret/i }).first().click();
		await page.fill("input[type=password]", "fake-key-for-e2e");
		await page.getByRole("button", { name: /^save$/i }).click();
		await page.getByText(/fingerprint/).first().waitFor();
		await go(page, BASE + "/");
		const welcome = await page.getByText(/paste your first provider key/i).count();
		return { ok: welcome === 0, note: `onboarding visible after save: ${welcome}` };
	});

	await step("create a (mock) job -> job reaches done -> final video downloads", async () => {
		await go(page, BASE + "/create");
		await page.locator("#create-topic").fill("Why altitude changes your coffee");
		await page.getByRole("button", { name: /submit mock job/i }).click();
		await page.waitForURL((u) => /^\/jobs\/job_/.test(u.pathname), { timeout: 20000 });
		const link = page.getByRole("link", { name: /^download$/i }).first();
		await link.waitFor({ timeout: 60000 });
		const href = await link.getAttribute("href");
		const res = await ctx.request.get(new URL(href, BASE).toString());
		const body = await res.body();
		return {
			ok: res.status() === 200 && body.length > 1000,
			note: `GET ${href.split("?")[0]} -> ${res.status()} ${body.length} bytes`,
		};
	});

	await step("A-2: logout, then the login FORM redirects to ?next=", async () => {
		await page.getByRole("button", { name: /log out/i }).first().click();
		await page.waitForURL((u) => u.pathname === "/login");
		posts.length = 0;
		await go(page, BASE + "/login?next=/jobs");
		await page.fill("input[name=email]", EMAIL);
		await page.fill("input[name=password]", PASSWORD);
		await page.getByRole("button", { name: /^log in$/i }).click();
		await page.waitForURL((u) => u.pathname === "/jobs");
		return { ok: !posts.some((s) => s >= 500), note: `POST statuses ${JSON.stringify(posts)}` };
	});

	await step("A-2: wrong password shows an inline error (no 500) and keeps email + next", async () => {
		await ctx.clearCookies();
		posts.length = 0;
		await go(page, BASE + "/login?next=/jobs");
		await page.fill("input[name=email]", EMAIL);
		await page.fill("input[name=password]", "definitely-the-wrong-one");
		await page.getByRole("button", { name: /^log in$/i }).click();
		await page.waitForSelector(".alert.error");
		const email = await page.inputValue("input[name=email]");
		const next = await page.locator("input[name=next]").inputValue();
		return {
			ok: email === EMAIL && next === "/jobs" && posts.length > 0 && posts.every((s) => s < 500),
			note: `statuses ${JSON.stringify(posts)} email kept=${email === EMAIL} next=${next}`,
		};
	});

	for (const nx of ["//evil.example/x", "/\\evil.example", "https://evil.example"]) {
		await step(`open redirect: ?next=${nx} stays on this origin`, async () => {
			await ctx.clearCookies();
			await go(page, BASE + "/login?next=" + encodeURIComponent(nx));
			await page.fill("input[name=email]", EMAIL);
			await page.fill("input[name=password]", PASSWORD);
			await page.getByRole("button", { name: /^log in$/i }).click();
			await page.waitForURL((u) => u.pathname !== "/login");
			return { ok: new URL(page.url()).origin === new URL(BASE).origin, note: page.url() };
		});
	}
	await ctx.close();

	// ---------------- mobile 390 ----------------
	const mctx = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true });
	const mp = await mctx.newPage();
	mp.setDefaultTimeout(10000);

	await step("mobile: log in through the form", async () => {
		await go(mp, BASE + "/login?next=/jobs");
		await mp.fill("input[name=email]", EMAIL);
		await mp.fill("input[name=password]", PASSWORD);
		await mp.getByRole("button", { name: /^log in$/i }).click();
		await mp.waitForURL((u) => u.pathname === "/jobs");
		return { ok: true };
	});

	await step("mobile: no horizontal overflow on any main route", async () => {
		const wide = [];
		for (const path of ["/", "/jobs", "/create", "/bible", "/assets", "/credentials", "/integrations"]) {
			await go(mp, BASE + path);
			const sw = await mp.evaluate(() => document.documentElement.scrollWidth);
			if (sw > 390) wide.push(`${path}=${sw}`);
		}
		return { ok: wide.length === 0, note: wide.join(", ") };
	});

	await go(mp, BASE + "/");
	await step("A-6: menu button toggles aria-expanded", async () => {
		const btn = mp.getByRole("button", { name: /menu/i }).first();
		const before = await btn.getAttribute("aria-expanded");
		await btn.click();
		const after = await btn.getAttribute("aria-expanded");
		const linkVisible = await mp.getByRole("link", { name: "Jobs", exact: true }).isVisible();
		return { ok: before === "false" && after === "true" && linkVisible, note: `${before}->${after} jobsLink=${linkVisible}` };
	});

	await step("A-6: Escape closes the menu and returns focus to the button", async () => {
		await mp.keyboard.press("Escape");
		const btn = mp.getByRole("button", { name: /menu/i }).first();
		const exp = await btn.getAttribute("aria-expanded");
		const focused = await mp.evaluate(() => document.activeElement?.getAttribute("aria-controls"));
		return { ok: exp === "false" && focused === "topbar-panel", note: `expanded=${exp} focus=${focused}` };
	});

	await step("A-6: navigate to Create through the menu; menu closes afterwards", async () => {
		await mp.getByRole("button", { name: /menu/i }).first().click();
		await mp.getByRole("link", { name: "Create", exact: true }).click();
		await mp.waitForURL((u) => u.pathname === "/create");
		const exp = await mp.getByRole("button", { name: /menu/i }).first().getAttribute("aria-expanded");
		return { ok: exp === "false", note: `expanded=${exp}` };
	});

	await step("A-6: log out is reachable from the menu", async () => {
		await mp.getByRole("button", { name: /menu/i }).first().click();
		return { ok: await mp.getByRole("button", { name: /log out/i }).first().isVisible() };
	});
	await mctx.close();
} finally {
	await browser.close();
	fs.writeFileSync(OUT, JSON.stringify({ steps }, null, 2));
}
process.exit(steps.every((s) => s.ok) ? 0 : 1);
