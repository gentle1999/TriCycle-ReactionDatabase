import { expect, test, type Locator, type Page } from "@playwright/test";

async function waitForMolecules(page: import("@playwright/test").Page): Promise<void> {
  await page.waitForFunction(() => {
    const canvases = [...document.querySelectorAll<HTMLCanvasElement>(".molecule-canvas canvas")];
    return canvases.length >= 3 && canvases.every((canvas) => canvas.width > 100 && canvas.height > 100);
  });
  await expect(page.locator(".molecule-state.is-error")).toHaveCount(0);
  await expect(page.locator(".molecule-state")).toHaveCount(0);
}

async function allCanvasesHaveDrawing(page: import("@playwright/test").Page): Promise<boolean> {
  return page.locator(".molecule-canvas canvas").evaluateAll((canvases) =>
    canvases.every((element) => {
      const canvas = element as HTMLCanvasElement;
      const renderer = canvas.closest<HTMLElement>("[data-renderer]")?.dataset.renderer;
      if (
        renderer === "chemdoodle-transform-3d"
        || renderer === "chemdoodle-movie-3d"
        || renderer === "chemdoodle-ts-mode-3d"
      ) {
        const gl =
          canvas.getContext("webgl") ??
          (canvas.getContext("experimental-webgl") as WebGLRenderingContext | null);
        if (!gl) return false;
        const pixels = new Uint8Array(canvas.width * canvas.height * 4);
        gl.finish();
        gl.readPixels(0, 0, canvas.width, canvas.height, gl.RGBA, gl.UNSIGNED_BYTE, pixels);
        let moleculePixels = 0;
        for (let offset = 0; offset < pixels.length; offset += 16) {
          if (
            pixels[offset + 3] > 0 &&
            Math.abs(pixels[offset] - 251) +
              Math.abs(pixels[offset + 1] - 252) +
              Math.abs(pixels[offset + 2] - 250) >
              24
          ) {
            moleculePixels += 1;
          }
        }
        return moleculePixels > 10;
      }
      const context = canvas.getContext("2d");
      if (!context) return false;
      const { data } = context.getImageData(0, 0, canvas.width, canvas.height);
      let darkPixels = 0;
      for (let offset = 0; offset < data.length; offset += 16) {
        if (
          data[offset + 3] > 0 &&
          data[offset] + data[offset + 1] + data[offset + 2] < 650
        ) {
          darkPixels += 1;
        }
      }
      return darkPixels > 10;
    }),
  );
}

async function analyticsCanvasesHaveDrawing(
  page: import("@playwright/test").Page,
): Promise<boolean> {
  return page.locator(".analytics-chart canvas").evaluateAll((canvases) =>
    canvases.length === 5 && canvases.every((element) => {
      const canvas = element as HTMLCanvasElement;
      const context = canvas.getContext("2d");
      if (!context || canvas.width < 200 || canvas.height < 200) return false;
      const pixels = context.getImageData(0, 0, canvas.width, canvas.height).data;
      let painted = 0;
      for (let offset = 0; offset < pixels.length; offset += 16) {
        if (pixels[offset + 3] > 0) painted += 1;
      }
      return painted > 20;
    }),
  );
}

async function webGlCanvasHasDrawing(renderer: Locator): Promise<boolean> {
  return renderer.locator("canvas").evaluate((canvas: HTMLCanvasElement) => {
    const gl = canvas.getContext("webgl")
      ?? canvas.getContext("experimental-webgl") as WebGLRenderingContext | null;
    if (!gl || gl.isContextLost()) return false;
    const pixels = new Uint8Array(canvas.width * canvas.height * 4);
    gl.finish();
    gl.readPixels(0, 0, canvas.width, canvas.height, gl.RGBA, gl.UNSIGNED_BYTE, pixels);
    let moleculePixels = 0;
    for (let offset = 0; offset < pixels.length; offset += 16) {
      if (
        pixels[offset + 3] > 0
        && Math.abs(pixels[offset] - 251)
          + Math.abs(pixels[offset + 1] - 252)
          + Math.abs(pixels[offset + 2] - 250) > 24
      ) moleculePixels += 1;
    }
    return moleculePixels > 10;
  });
}

async function dragChangesWebGlCanvas(page: Page, canvas: Locator): Promise<boolean> {
  const readPixels = (): Promise<number[]> => canvas.evaluate((element: HTMLCanvasElement) => {
    const gl = element.getContext("webgl")
      ?? element.getContext("experimental-webgl") as WebGLRenderingContext | null;
    if (!gl) return [];
    gl.finish();
    const pixels = new Uint8Array(element.width * element.height * 4);
    gl.readPixels(0, 0, element.width, element.height, gl.RGBA, gl.UNSIGNED_BYTE, pixels);
    return [...pixels];
  });
  const beforeDrag = await readPixels();
  const canvasBox = await canvas.boundingBox();
  expect(canvasBox).not.toBeNull();
  const centerX = (canvasBox?.x ?? 0) + (canvasBox?.width ?? 0) / 2;
  const centerY = (canvasBox?.y ?? 0) + (canvasBox?.height ?? 0) / 2;
  await page.mouse.move(centerX, centerY);
  await page.mouse.down();
  await page.mouse.move(centerX + 100, centerY + 35, { steps: 8 });
  await page.mouse.up();
  const afterDrag = await readPixels();
  return afterDrag.some((value, index) => value !== beforeDrag[index]);
}

async function forceWebGlRecovery(renderer: Locator): Promise<void> {
  await expect(renderer).toHaveAttribute("data-webgl-state", "ready");
  const canvas = renderer.locator("canvas");
  const previousCanvasId = await canvas.getAttribute("id");
  const previousRecoveryCount = Number(await renderer.getAttribute("data-webgl-recovery-count"));
  const contextLost = await canvas.evaluate((element: HTMLCanvasElement) => {
    const gl = element.getContext("webgl")
      ?? element.getContext("experimental-webgl") as WebGLRenderingContext | null;
    const extension = gl?.getExtension("WEBGL_lose_context");
    if (!extension) return false;
    extension.loseContext();
    return true;
  });
  expect(contextLost).toBe(true);
  await expect(renderer).toHaveAttribute(
    "data-webgl-recovery-count",
    String(previousRecoveryCount + 1),
  );
  await expect(renderer).toHaveAttribute("data-webgl-state", "ready", { timeout: 10_000 });
  await expect(renderer.locator("canvas")).not.toHaveAttribute("id", previousCanvasId ?? "");
  expect(await webGlCanvasHasDrawing(renderer)).toBe(true);
}

async function findArtifactWithMultipleFrames(page: import("@playwright/test").Page): Promise<{ id: string }> {
  const projectId = "00000000-0000-7000-8000-000000000201";
  const response = await page.request.get(`/api/artifacts?limit=50&offset=0&project_id=${projectId}`);
  expect(response.ok()).toBe(true);
  const payload = await response.json() as { items: Array<{ id: string }> };

  for (const artifact of payload.items) {
    const framesResponse = await page.request.get(
      `/api/calculation-frames?limit=2&offset=0&project_id=${projectId}&artifact_file_id=${artifact.id}`,
    );
    expect(framesResponse.ok()).toBe(true);
    const frames = await framesResponse.json() as { page: { total: number } };
    if (frames.page.total > 1) return artifact;
  }
  throw new Error("当前测试数据中没有包含多个计算帧的原始文件");
}

async function findLogicalReactionPage(
  page: import("@playwright/test").Page,
  labelFragment: string,
): Promise<{
  pageNumber: number;
  reaction: { label: string | null; reactant_topology_ids: string[] };
}> {
  const projectId = "00000000-0000-7000-8000-000000000201";
  const batchSize = 200;
  let offset = 0;
  while (true) {
    const response = await page.request.get(
      `/api/logical-reactions?limit=${batchSize}&offset=${offset}&project_id=${projectId}`,
    );
    expect(response.ok()).toBe(true);
    const payload = await response.json() as {
      items: Array<{ label: string | null; reactant_topology_ids: string[] }>;
      page: { total: number };
    };
    const index = payload.items.findIndex((item) => item.label?.includes(labelFragment));
    if (index >= 0) {
      return {
        pageNumber: Math.floor((offset + index) / 12) + 1,
        reaction: payload.items[index],
      };
    }
    offset += payload.items.length;
    if (!payload.items.length || offset >= payload.page.total) break;
  }
  throw new Error(`未找到标签包含 ${labelFragment} 的逻辑反应`);
}

async function mockReactionCatalog(page: Page, total: number): Promise<void> {
  const projectId = "00000000-0000-7000-8000-000000000201";
  const response = await page.request.get(
    `/api/logical-reactions?limit=1&offset=0&project_id=${projectId}`,
  );
  expect(response.ok()).toBe(true);
  const payload = await response.json() as { items: Array<Record<string, unknown>> };
  const source = payload.items[0];
  expect(source).toBeTruthy();
  const fixtures = Array.from({ length: total }, (_, index) => ({
    ...source,
    id: `00000000-0000-7000-8001-${String(index + 1).padStart(12, "0")}`,
    reaction_key: `e2e-catalog-${String(index + 1).padStart(3, "0")}`,
    label: `E2E catalog reaction ${index + 1}`,
  }));
  await page.route("**/api/logical-reactions?*", async (route) => {
    const url = new URL(route.request().url());
    if (url.searchParams.get("limit") !== "12") {
      await route.continue();
      return;
    }
    const offset = Number(url.searchParams.get("offset") ?? "0");
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        items: fixtures.slice(offset, offset + 12),
        page: { total, limit: 12, offset },
      }),
    });
  });
}

function uploadBatchFixture(
  id: string,
  values: Partial<Record<string, unknown>> = {},
): Record<string, unknown> {
  return {
    id,
    created_at: "2026-08-20T08:00:00Z",
    updated_at: "2026-08-20T08:00:00Z",
    project_id: "00000000-0000-7000-8000-000000000201",
    created_by_user_id: "00000000-0000-7000-8000-000000000002",
    artifact_kind: "auxiliary",
    status: "active",
    shared_metadata: { campaign: "e2e-screen" },
    total_count: 3,
    total_bytes: 30,
    succeeded_count: 0,
    failed_count: 0,
    cancelled_count: 0,
    uploading_count: 0,
    ...values,
  };
}

function uploadBatchItemFixture(
  clientFileId: string,
  filename: string,
  values: Partial<Record<string, unknown>> = {},
): Record<string, unknown> {
  return {
    id: crypto.randomUUID(),
    created_at: "2026-08-20T08:00:00Z",
    updated_at: "2026-08-20T08:00:00Z",
    client_file_id: clientFileId,
    position: 0,
    original_filename: filename,
    relative_path: filename,
    size_bytes: 10,
    media_type: "text/plain",
    status: "succeeded",
    attempt_count: 1,
    artifact_file_id: "00000000-0000-7000-8000-000000000905",
    error_code: null,
    error_message: null,
    metadata: { campaign: "e2e-screen" },
    ...values,
  };
}

test("thermodynamic statistics render charts and export the current project", async ({ page }, testInfo) => {
  const projectId = "00000000-0000-7000-8000-000000000201";
  const mappedResponse = await page.request.get(
    `/api/mapped-reactions?limit=1&offset=0&project_id=${projectId}`,
  );
  expect(mappedResponse.ok()).toBe(true);
  const mappedPayload = await mappedResponse.json() as { items: Array<{ id: string }> };
  const scatterReactionId = mappedPayload.items[0]?.id;
  if (!scatterReactionId) throw new Error("当前测试数据中没有映射反应");
  const runtimeErrors: string[] = [];
  page.on("pageerror", (error) => runtimeErrors.push(error.message));
  page.on("console", (message) => {
    if (message.type() === "error") runtimeErrors.push(message.text());
  });
  await page.route("**/api/auth/me", async (route) => {
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        id: "00000000-0000-7000-8000-000000000002",
        display_name: "Development User",
        primary_email: "developer@localhost",
        is_service_account: false,
        identity: { issuer: "development", subject: "development" },
        projects: [{
          project_id: projectId,
          project_slug: "default",
          project_name: "默认研究项目",
          organization_id: "00000000-0000-7000-8000-000000000101",
          organization_slug: "development",
          organization_name: "Development",
          organization_role: "owner",
          project_role: "manager",
          permissions: ["artifact:read"],
        }],
      }),
    });
  });
  await page.route("**/health/ready", async (route) => {
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({ database: "tricycle", postgresql_version: "17", rdkit_extension_version: "2025.03" }),
    });
  });
  await page.route("**/api/mapped-reactions/thermodynamics/statistics*", async (route) => {
    expect(new URL(route.request().url()).searchParams.get("project_id")).toBe(projectId);
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        mapped_reaction_count: 72,
        profile_count: 84,
        activation_profile_count: 61,
        reaction_profile_count: 74,
        complete_profile_count: 55,
        activation_gibbs_free_energy_kcal_mol: [
          { lower: 8, upper: 16, count: 7 },
          { lower: 16, upper: 24, count: 21 },
          { lower: 24, upper: 32, count: 25 },
          { lower: 32, upper: 40, count: 8 },
        ],
        reaction_gibbs_free_energy_kcal_mol: [
          { lower: -35, upper: -20, count: 14 },
          { lower: -20, upper: -5, count: 30 },
          { lower: -5, upper: 10, count: 22 },
          { lower: 10, upper: 25, count: 8 },
        ],
        level_of_theory: [
          { label: "B3LYP/Def2SVP", count: 49 },
          { label: "wB97M-V/Def2TZVPP + B3LYP/Def2SVP", count: 23 },
          { label: "M06-2X/6-31G(d)", count: 12 },
        ],
        temperature_kelvin: [
          { label: "298.15 K", count: 79 },
          { label: "373.15 K", count: 5 },
        ],
        scatter: [
          { mapped_reaction_id: scatterReactionId, mapped_reaction_smiles: "[C:1]>>[C:1]", activation_gibbs_free_energy_kcal_mol: 18, reaction_gibbs_free_energy_kcal_mol: -12 },
          { mapped_reaction_id: "2", mapped_reaction_smiles: "[N:1]>>[N:1]", activation_gibbs_free_energy_kcal_mol: 27, reaction_gibbs_free_energy_kcal_mol: 4 },
          { mapped_reaction_id: "3", mapped_reaction_smiles: "[O:1]>>[O:1]", activation_gibbs_free_energy_kcal_mol: 34, reaction_gibbs_free_energy_kcal_mol: -2 },
        ],
      }),
    });
  });
  await page.route("**/api/mapped-reactions/thermodynamics/export.csv*", async (route) => {
    expect(new URL(route.request().url()).searchParams.get("project_id")).toBe(projectId);
    await route.fulfill({
      contentType: "text/csv; charset=utf-8",
      headers: { "Content-Disposition": "attachment; filename=mapped-reaction-thermodynamics.csv" },
      body: "mapped_reaction_smiles,activation_gibbs_free_energy_kcal_mol,reaction_gibbs_free_energy_kcal_mol\n[C:1]>>[C:1],18,-12\n",
    });
  });

  await page.setViewportSize({ width: 1440, height: 960 });
  await page.goto(`/statistics?project_id=${projectId}`);
  await expect(page.getByRole("heading", { name: "反应路径分布统计" })).toBeVisible();
  await expect(page.locator(".analytics-panel")).toHaveCount(5);
  await expect(page.locator(".analytics-chart canvas")).toHaveCount(5);
  await expect.poll(() => analyticsCanvasesHaveDrawing(page)).toBe(true);
  await expect(page.locator(".analytics-kpis")).toContainText("72");

  const scatterCanvas = page.locator(".analytics-scatter-chart canvas");
  const firstPoint = await scatterCanvas.evaluate((element) => {
    const canvas = element as HTMLCanvasElement;
    const context = canvas.getContext("2d");
    if (!context) return { x: 0, y: 0 };
    const data = context.getImageData(0, 0, canvas.width, canvas.height).data;
    const pixels: Array<{ x: number; y: number }> = [];
    for (let y = 0; y < canvas.height; y += 1) {
      for (let x = 0; x < canvas.width; x += 1) {
        const offset = (y * canvas.width + x) * 4;
        if (data[offset] < 100 && data[offset + 1] > 65 && data[offset + 1] < 140 && data[offset + 2] > 100) {
          pixels.push({ x, y });
        }
      }
    }
    const lowestY = Math.max(...pixels.map((point) => point.y));
    const pointPixels = pixels.filter((point) => point.y >= lowestY - 10);
    return {
      x: pointPixels.reduce((sum, point) => sum + point.x, 0) / pointPixels.length,
      y: pointPixels.reduce((sum, point) => sum + point.y, 0) / pointPixels.length,
    };
  });
  const scatterBox = await scatterCanvas.boundingBox();
  expect(scatterBox).not.toBeNull();
  await scatterCanvas.hover({ position: firstPoint });
  const pointTooltip = page.locator(".thermo-scatter-tooltip");
  await expect(pointTooltip).toBeVisible();
  await expect(pointTooltip).toContainText("[C:1]>>[C:1]");
  await expect(pointTooltip).toContainText("18.00 kcal/mol");
  await expect(pointTooltip).toContainText("-12.00 kcal/mol");
  await expect(pointTooltip.getByRole("link", { name: "映射反应详情 ↗" })).toHaveAttribute(
    "href",
    `/mapped-reactions/${scatterReactionId}?project_id=${projectId}`,
  );
  await scatterCanvas.click({ position: firstPoint });
  await expect(page).toHaveURL(`/mapped-reactions/${scatterReactionId}?project_id=${projectId}`);
  await page.goBack();
  await expect(page.getByRole("heading", { name: "反应路径分布统计" })).toBeVisible();

  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "导出热力学 CSV" }).click();
  expect((await download).suggestedFilename()).toBe("mapped-reaction-thermodynamics-default.csv");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("desktop-thermodynamic-statistics.png"), fullPage: true });

  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("mobile-thermodynamic-statistics.png"), fullPage: true });
  expect(runtimeErrors).toEqual([]);
});

test("reaction cards render paths and open mapped/frame detail", async ({ page }, testInfo) => {
  const runtimeErrors: string[] = [];
  page.on("pageerror", (error) => runtimeErrors.push(error.message));
  page.on("console", (message) => {
    if (message.type() === "error") runtimeErrors.push(message.text());
  });

  await page.setViewportSize({ width: 1440, height: 960 });
  await page.goto("/");
  await expect(page.locator("#reaction-view-title")).toBeVisible();
  await expect(page.locator(".metrics-band > div")).toHaveCount(5);
  await expect(page.locator(".reaction-path-card").first()).toBeVisible();
  await waitForMolecules(page);
  await expect(page.locator('[data-renderer="chemdoodle-transform-3d"]')).not.toHaveCount(0);
  await expect(page.locator('[data-representation="enhanced-wireframe"]')).not.toHaveCount(0);
  expect(await page.locator(".reaction-path-card").count()).toBeLessThanOrEqual(12);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);

  const firstReactionGroup = page.locator(".reaction-card-group").first();
  await expect(firstReactionGroup.getByRole("link", { name: /在独立页面打开反应路径/ })).toHaveAttribute("href", /\/reactions\//);
  const reactionCard = firstReactionGroup.locator(".reaction-path-card");
  const directLink = firstReactionGroup.locator(".reaction-card-direct-link");
  const cardLayout = await reactionCard.evaluate((card) => {
    const direct = card.querySelector<HTMLElement>(".reaction-card-direct-link");
    const hash = card.querySelector<HTMLElement>(".reaction-card-footer code");
    if (!direct || !hash) throw new Error("reaction card controls are missing");
    const cardRect = card.getBoundingClientRect();
    const directRect = direct.getBoundingClientRect();
    const hashRect = hash.getBoundingClientRect();
    return {
      directTop: directRect.top,
      cardTop: cardRect.top,
      directBottom: directRect.bottom,
      hashTop: hashRect.top,
    };
  });
  expect(cardLayout.directTop).toBeGreaterThanOrEqual(cardLayout.cardTop);
  expect(cardLayout.directBottom).toBeLessThan(cardLayout.hashTop);
  await expect(firstReactionGroup.locator(".reaction-card-toggle")).toHaveAttribute("aria-label", "展开路径");
  await firstReactionGroup.locator(".reaction-path-card").click();
  await expect(firstReactionGroup.locator(".reaction-card-toggle")).toHaveAttribute("aria-label", "收起路径");
  expect(new URL(page.url()).searchParams.get("preview_reaction")).not.toBeNull();
  await expect(firstReactionGroup.locator(".mapped-reaction-expansion")).toBeVisible();
  await expect(page.locator(".reaction-results > .mapped-reaction-expansion")).toHaveCount(0);
  await firstReactionGroup.locator(".reaction-path-card").click();
  await expect(firstReactionGroup.locator(".mapped-reaction-expansion")).toHaveCount(0);

  await firstReactionGroup.locator(".reaction-path-card").click();
  const expansion = firstReactionGroup.locator(".mapped-reaction-expansion");
  await expect(expansion).toBeVisible();
  await expect(expansion.locator(".mapped-reaction-smiles")).toContainText(":1");
  await expect(expansion.locator(".mapped-reaction-smiles")).toContainText(">>");
  await expect(expansion.locator('[data-atom-mapped="true"]')).not.toHaveCount(0);
  await expect(expansion.locator('[data-atom-map-start="1"]')).not.toHaveCount(0);
  await expect(expansion.locator(".node-step strong")).toHaveText(["反应物", "过渡态", "产物"]);
  await expansion.locator(".node-step").first().click();
  await expect(expansion.locator(".geometry-component-row").first()).toBeVisible();
  await expect(expansion.locator(".geometry-item").first()).toBeVisible();
  await page.locator(".mapped-reaction-expansion .frame-links button").first().click();
  await expect(page.getByRole("heading", { name: "帧详情" })).toBeVisible();
  await expect(page.locator('.detail-drawer .drawer-header a[aria-label="在独立页面打开计算帧"]')).toHaveAttribute("href", /\/calculations\//);
  await expect(
    page.locator('.detail-drawer [data-renderer="chemdoodle-transform-3d"]'),
  ).toHaveCount(1);
  await expect(page.locator(".detail-drawer .molecule-state")).toHaveCount(0);
  expect(await allCanvasesHaveDrawing(page)).toBe(true);
  const convergenceTable = page.getByRole("table", { name: "优化收敛性指标" });
  await expect(convergenceTable).toBeVisible();
  await expect(convergenceTable.getByRole("columnheader")).toHaveText(["指标", "实际值", "参考值", "判定"]);
  await expect(convergenceTable.getByRole("row")).toHaveCount(6);
  const rmsForceRow = convergenceTable.getByRole("row").filter({ hasText: "RMS 力" });
  await expect(rmsForceRow).toContainText(/已满足|未满足|未判定/);
  await expect(rmsForceRow.locator("code")).toHaveCount(2);
  await expect(rmsForceRow.locator("code").nth(0)).not.toHaveText("—");
  await expect(rmsForceRow.locator("code").nth(1)).not.toHaveText("—");
  const firstArray = page.locator(".detail-drawer .array-list-item").first();
  await expect(firstArray).toBeVisible();
  await firstArray.locator(".array-toggle").click();
  await expect(firstArray.locator(".array-preview-values")).toBeVisible();
  await expect(firstArray.locator(".array-preview-values")).not.toBeEmpty();
  await expect(firstArray.locator(".array-download")).toHaveAttribute("href", /scientific-arrays/);
  await page.screenshot({ path: testInfo.outputPath("desktop-frame-detail.png"), fullPage: true });

  const artifactDetailResponse = page.waitForResponse((response) =>
    /\/api\/artifacts\/[0-9a-f-]+$/.test(new URL(response.url()).pathname),
  );
  await page.getByRole("link", { name: "查看原始文件" }).click();
  expect((await artifactDetailResponse).ok()).toBe(true);
  await expect(page).toHaveURL(/\/artifacts\/[0-9a-f-]+/);
  await expect(page.getByRole("heading", { name: "原始文件详情" })).toBeVisible();
  await expect(page.locator(".artifact-detail-identity")).toBeVisible();

  expect(runtimeErrors).toEqual([]);
});

test("reaction IDs open a standalone mapped path page", async ({ page }, testInfo) => {
  const projectId = "00000000-0000-7000-8000-000000000201";
  const response = await page.request.get(`/api/logical-reactions?limit=1&offset=0&project_id=${projectId}`);
  expect(response.ok()).toBe(true);
  const payload = await response.json() as { items: Array<{ id: string }> };
  const reaction = payload.items[0];
  expect(reaction).toBeTruthy();

  await page.setViewportSize({ width: 1440, height: 960 });
  await page.goto(`/reactions/${reaction.id}?project_id=${projectId}`);
  await expect(page.locator("#reaction-detail-title")).toBeVisible();
  await expect(page.locator(".reaction-detail-body .mapped-reaction-expansion")).toBeVisible();
  await expect(page.locator(".reaction-detail-body .mapped-reaction-smiles")).toContainText(">>");
  const mappedHref = await page.getByRole("link", { name: "映射路径页" }).getAttribute("href");
  expect(mappedHref).toMatch(/\/mapped-reactions\//);
  await page.goto(mappedHref ?? "");
  await expect(page).toHaveURL(/\/mapped-reactions\//);
  await expect(page.locator(".reaction-detail-body .mapped-reaction-expansion")).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("desktop-reaction-detail.png"), fullPage: true });
});

test("complete mapped thermodynamics render the precursor TS product energy surface", async ({ page }, testInfo) => {
  const projectId = "00000000-0000-7000-8000-000000000201";
  const response = await page.request.get(
    `/api/mapped-reactions?limit=50&offset=0&project_id=${projectId}`,
  );
  expect(response.ok()).toBe(true);
  const payload = await response.json() as {
    items: Array<{
      id: string;
      minimum_activation_gibbs_free_energy_kcal_mol: number | null;
      minimum_reaction_gibbs_free_energy_kcal_mol: number | null;
    }>;
  };
  const mappedReaction = payload.items.find((item) =>
    item.minimum_activation_gibbs_free_energy_kcal_mol !== null
    && item.minimum_reaction_gibbs_free_energy_kcal_mol !== null,
  );
  expect(mappedReaction).toBeTruthy();
  await page.setViewportSize({ width: 1440, height: 960 });
  await page.goto(`/mapped-reactions/${mappedReaction?.id}?project_id=${projectId}`);
  const potentialEnergy = page.locator(".reaction-potential-energy");
  await expect(potentialEnergy).toBeVisible({ timeout: 20_000 });
  await expect(potentialEnergy.locator(".potential-stage-label")).toHaveText(["前体", "TS", "后体"]);
  await expect(potentialEnergy.locator(".reaction-potential-energy-metrics")).toContainText(
    mappedReaction?.minimum_activation_gibbs_free_energy_kcal_mol?.toFixed(2) ?? "",
  );
  await expect(potentialEnergy.locator(".reaction-potential-energy-metrics")).toContainText(
    mappedReaction?.minimum_reaction_gibbs_free_energy_kcal_mol?.toFixed(2) ?? "",
  );
  const chartCanvas = potentialEnergy.locator("canvas");
  await expect(chartCanvas).toBeVisible();
  expect(await chartCanvas.evaluate((canvas: HTMLCanvasElement) => {
    const context = canvas.getContext("2d");
    if (!context) return false;
    const pixels = context.getImageData(0, 0, canvas.width, canvas.height).data;
    let nonTransparentPixels = 0;
    for (let offset = 3; offset < pixels.length; offset += 16) {
      if (pixels[offset] > 0) nonTransparentPixels += 1;
    }
    return nonTransparentPixels > 500;
  })).toBe(true);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await potentialEnergy.screenshot({ path: testInfo.outputPath("reaction-potential-energy-echarts.png") });
});

test("TS frame detail interpolates persisted signed mode anchors", async ({ page }) => {
  const runtimeErrors: string[] = [];
  page.on("pageerror", (error) => runtimeErrors.push(error.message));
  page.on("console", (message) => {
    if (message.type() === "error") runtimeErrors.push(message.text());
  });

  await page.setViewportSize({ width: 1440, height: 960 });
  await page.goto("/");
  const reactionGroup = page.locator(".reaction-card-group").first();
  await reactionGroup.locator(".reaction-path-card").click();
  const expansion = reactionGroup.locator(".mapped-reaction-expansion");
  await expect(expansion).toBeVisible();
  const transitionStateStep = expansion.locator(".node-step").filter({ hasText: "过渡态" });
  await expect(transitionStateStep).toHaveCount(1);
  await transitionStateStep.click();
  const transitionStateFrames = expansion.locator(".node-detail .frame-links button");
  await expect(transitionStateFrames.first()).toBeVisible();
  const modeRenderer = page.locator('[data-renderer="chemdoodle-ts-mode-3d"]');
  let openedModeFrame = false;
  for (let index = 0; index < await transitionStateFrames.count(); index += 1) {
    await transitionStateFrames.nth(index).click();
    await expect(page.getByRole("heading", { name: "帧详情" })).toBeVisible();
    await expect(page.locator(".detail-drawer .drawer-loading")).toHaveCount(0);
    if (await modeRenderer.count()) {
      openedModeFrame = true;
      break;
    }
    await page.locator('.detail-drawer .drawer-header button[aria-label="关闭"]').click();
    await expect(page.getByRole("heading", { name: "帧详情" })).toHaveCount(0);
  }
  expect(openedModeFrame).toBe(true);
  await expect(modeRenderer).toBeVisible();
  await expect(modeRenderer).toHaveAttribute("data-frame-count", "21", { timeout: 20_000 });
  await expect(modeRenderer.locator(".molecule-state")).toHaveCount(0);
  await expect(modeRenderer.locator("canvas")).toHaveCSS("border-top-width", "0px");
  await modeRenderer.getByRole("slider", { name: "虚频模式位置" }).fill("10");
  await expect(modeRenderer.locator(".frame-movie-position")).toHaveText("mode 0.000");
  expect(await webGlCanvasHasDrawing(modeRenderer)).toBe(true);

  await modeRenderer.getByRole("button", { name: "负方向起点" }).click();
  await expect(modeRenderer.locator(".frame-movie-position")).toHaveText("mode -1.000");
  await modeRenderer.getByRole("button", { name: "正方向终点" }).click();
  await expect(modeRenderer.locator(".frame-movie-position")).toHaveText("mode 1.000");
  await forceWebGlRecovery(modeRenderer);
  expect(await webGlCanvasHasDrawing(modeRenderer)).toBe(true);
  expect(runtimeErrors).toEqual([]);
});

test("artifact deep link keeps its visible filter in the sidebar", async ({ page }) => {
  const projectId = "00000000-0000-7000-8000-000000000201";
  const response = await page.request.get(`/api/artifacts?limit=1&offset=0&project_id=${projectId}`);
  expect(response.ok()).toBe(true);
  const payload = await response.json() as { items: Array<{ id: string; original_filename: string }> };
  expect(payload.items).toHaveLength(1);
  const artifact = payload.items[0];

  await page.setViewportSize({ width: 1440, height: 960 });
  await page.goto(`/artifacts?project_id=${projectId}&artifact_id=${artifact.id}`);
  await expect(page.locator(".artifact-browser")).toBeVisible();
  await expect(page.locator(".metrics-band > div")).toHaveCount(5);
  await expect(page.locator(".artifact-filter-sidebar")).toBeVisible();
  await expect(page.locator(".artifact-results")).toBeVisible();
  await expect(page.getByRole("searchbox", { name: "按文件 ID、SHA-256 或名称筛选" })).toHaveValue(artifact.id);
  await expect(page.locator(".artifact-row")).toHaveCount(1);
  await expect(page.locator(".artifact-row")).toContainText(artifact.original_filename);
  await expect(page.locator(".filter-result-count")).toContainText("1");
});

test("artifact quick and advanced filters preserve filename semantics", async ({ page }, testInfo) => {
  const projectId = "00000000-0000-7000-8000-000000000201";
  const firstArtifactResponse = await page.request.get(`/api/artifacts?limit=1&offset=0&project_id=${projectId}`);
  expect(firstArtifactResponse.ok()).toBe(true);
  const firstArtifactPayload = await firstArtifactResponse.json() as { items: Array<{ content_sha256: string }> };
  const sha256 = firstArtifactPayload.items[0]?.content_sha256;
  expect(sha256).toMatch(/^[0-9a-f]{64}$/);

  await page.setViewportSize({ width: 1440, height: 960 });
  await page.goto(`/artifacts?project_id=${projectId}`);
  const quickInput = page.getByRole("searchbox", { name: "按文件 ID、SHA-256 或名称筛选" });
  await quickInput.fill("output.log");
  await expect(page.locator(".artifact-filter-sidebar .query-validation-indicator")).toHaveCount(0);
  const filenameResponsePromise = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return url.pathname === "/api/artifacts" && url.searchParams.get("original_filename_contains") === "output.log";
  });
  await page.getByRole("button", { name: "查询", exact: true }).click();
  expect((await filenameResponsePromise).status()).toBe(200);

  await quickInput.fill("00000000-0000-7000-8000-0000000000");
  await expect(page.locator(".artifact-filter-sidebar .query-validation-indicator.is-invalid")).toHaveAttribute("title", "文件 ID 必须是 UUID 格式");

  await page.getByRole("button", { name: "高级筛选", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "高级筛选" });
  await expect(dialog).toBeVisible();
  const dialogBox = await dialog.boundingBox();
  expect(dialogBox).not.toBeNull();
  expect(Math.abs((dialogBox?.x ?? 0) + (dialogBox?.width ?? 0) / 2 - 720)).toBeLessThan(20);
  const fieldSelectors = dialog.locator('select[id^="artifact-advanced-query-field-"]');
  await fieldSelectors.first().selectOption("content_sha256");
  await dialog.getByRole("textbox", { name: "SHA-256条件值" }).fill(sha256 ?? "");
  await expect(dialog.locator(".query-validation-indicator.is-valid")).toHaveAttribute("title", "SHA-256 格式有效");
  await dialog.getByRole("button", { name: "添加条件" }).click();
  await fieldSelectors.nth(1).selectOption("artifact_kind");
  await dialog.getByRole("combobox", { name: "文件类型条件值" }).selectOption("calculation_output");
  const advancedResponsePromise = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return url.pathname === "/api/artifacts"
      && url.searchParams.get("content_sha256") === sha256
      && url.searchParams.get("artifact_kind") === "calculation_output";
  });
  await page.screenshot({ path: testInfo.outputPath("desktop-artifact-advanced-filter.png"), fullPage: true });
  await dialog.getByRole("button", { name: "应用高级筛选" }).click();
  expect((await advancedResponsePromise).status()).toBe(200);
  await expect(page.locator(".advanced-query-active")).toContainText("2 个条件");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);

  await page.getByRole("link", { name: "原始文件查询帮助", exact: true }).click();
  await expect(page).toHaveURL(/\/help\/artifact-query(?:\?|$)/);
  await expect(page.getByRole("heading", { name: "原始文件查询帮助" })).toBeVisible();
  await expect(page.getByText("普通文件名不会因为不是 UUID 或 SHA-256 而被判为错误")).toBeVisible();
});

test("artifact rows link to a standalone detail page", async ({ page }, testInfo) => {
  const projectId = "00000000-0000-7000-8000-000000000201";
  const artifactPointer = await findArtifactWithMultipleFrames(page);
  const response = await page.request.get(`/api/artifacts/${artifactPointer.id}`);
  expect(response.ok()).toBe(true);
  const artifact = await response.json() as {
    id: string;
    original_filename: string;
    preview_available: boolean;
    storage_status: string;
  };

  await page.setViewportSize({ width: 1440, height: 960 });
  await page.goto(`/artifacts?project_id=${projectId}&artifact_id=${artifact.id}`);
  const detailLink = page.getByRole("link", { name: `在独立页面打开原始文件 ${artifact.original_filename}` });
  await expect(detailLink).toHaveAttribute("href", new RegExp(`/artifacts/${artifact.id}`));
  await detailLink.click();

  await expect(page).toHaveURL(new RegExp(`/artifacts/${artifact.id}`));
  await expect(page.getByRole("heading", { name: "原始文件详情" })).toBeVisible();
  await expect(page.locator(".artifact-detail-identity")).toContainText(artifact.original_filename);
  await expect(page.getByRole("link", { name: "下载原始文件" })).toHaveAttribute("download", artifact.original_filename);
  await expect(page.getByRole("heading", { name: "关联计算帧" })).toBeVisible();
  await expect(page.locator(".frame-list-item").first()).toBeVisible();
  if (artifact.preview_available && artifact.storage_status === "available") {
    await expect(page.locator(".artifact-detail-preview")).toBeVisible();
    await expect(page.locator(".artifact-detail-preview code")).not.toBeEmpty();
  }
  await page.screenshot({ path: testInfo.outputPath("desktop-artifact-detail.png"), fullPage: true });

  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.locator(".artifact-detail-overview")).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("mobile-artifact-detail.png"), fullPage: true });
});

test("3D reaction geometry recovers its WebGL context and releases offscreen cards", async ({ page }) => {
  await mockReactionCatalog(page, 8);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/");
  const cards = page.locator(".reaction-path-card");
  await expect(cards.first()).toBeVisible();
  const firstRenderer = cards.first().locator('[data-renderer="chemdoodle-transform-3d"]');
  await expect(firstRenderer).toHaveCount(1);
  await forceWebGlRecovery(firstRenderer);

  await cards.last().scrollIntoViewIfNeeded();
  await expect(firstRenderer).toHaveCount(0);
  await cards.first().scrollIntoViewIfNeeded();
  const revivedRenderer = cards.first().locator('[data-renderer="chemdoodle-transform-3d"]');
  await expect(revivedRenderer).toHaveAttribute("data-webgl-state", "ready", { timeout: 10_000 });
  expect(await webGlCanvasHasDrawing(revivedRenderer)).toBe(true);
});

test("mobile reaction workspace has no page-level overflow", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  await expect(page.locator("#reaction-view-title")).toBeVisible();
  await expect(page.locator(".reaction-path-card").first()).toBeVisible();
  await waitForMolecules(page);

  expect(await allCanvasesHaveDrawing(page)).toBe(true);
  const firstReactionGroup = page.locator(".reaction-card-group").first();
  await firstReactionGroup.locator(".reaction-path-card").click();
  await expect(firstReactionGroup.locator(".mapped-reaction-expansion")).toBeVisible();
  await expect(firstReactionGroup.locator(".compact-equation .molecule-state")).toHaveCount(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("mobile-reaction.png"), fullPage: true });
});

test("reaction quick query submits a direct reaction SMILES", async ({ page }) => {
  await page.goto("/");
  const responsePromise = page.waitForResponse((response) =>
    response.url().includes("/api/logical-reactions")
    && response.request().method() === "GET"
    && response.url().includes("reaction_smarts=C%3DC%3E%3ECC"),
  );
  await page.getByRole("searchbox", { name: "反应 SMILES 快速查询" }).fill("C=C>>CC");
  await page.getByRole("button", { name: "查询", exact: true }).click();
  const response = await responsePromise;
  expect(response.status()).toBe(200);
  const payload = await response.json() as { page: { total: number } };
  expect(payload.page.total).toBeGreaterThan(0);
  await expect(page.getByRole("heading", { name: "反应结构查询结果" })).toBeVisible();
});

test("reaction query help is reachable from quick and advanced query controls", async ({ page }) => {
  await page.goto("/");
  const quickHelp = page.getByRole("link", { name: "反应查询帮助" }).first();
  await expect(quickHelp).toBeVisible();
  await quickHelp.click();
  await expect(page).toHaveURL(/\/help\/reaction-query(?:\?|$)/);
  await expect(page.getByRole("heading", { name: "反应查询帮助" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "RXN SMARTS 的写法" })).toBeVisible();
  await expect(page.getByRole("cell", { name: /完整 RXN SMARTS/ })).toBeVisible();
  await expect(page.getByRole("link", { name: /RDKit SMARTS 参考/ })).toHaveAttribute(
    "href",
    "https://www.rdkit.org/docs/RDKit_Book.html#smarts-support",
  );
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);

  await page.goto("/");
  await page.getByRole("button", { name: "高级查询", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "高级查询" });
  await expect(dialog.getByRole("link", { name: "查看反应查询帮助" })).toBeVisible();
});

test("reaction editor mirrors ChemDoodle reaction changes into its RXN field", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "高级查询", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "高级查询" });
  const editor = dialog.locator('.reaction-editor-field[data-renderer="chemdoodle-reaction"]');
  await expect(editor.frameLocator("iframe").locator("#editor-canvas")).toBeVisible();
  const conversion = await page.request.post("/api/chemistry/reactions", {
    data: { reaction_smiles: "C=C>>CC" },
  });
  expect(conversion.ok()).toBe(true);
  const payload = await conversion.json() as { rxn: string };
  await editor.frameLocator("iframe").locator("#editor-canvas").evaluate((_, rxn) => {
    window.parent.postMessage({
      protocol: "tricycle-chemdoodle-editor/1",
      type: "reactionChange",
      rxn,
    }, "*");
  }, payload.rxn);
  await expect(editor.locator(".topology-smiles-input input")).toHaveValue("C=C>>CC");
});

test("reaction structure inputs show validation errors while editing", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "高级查询", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "高级查询" });
  const reactionInput = dialog.locator('.reaction-editor-field .topology-smiles-input input');
  await reactionInput.fill("not a reaction");
  const reactionIndicator = dialog.locator(".query-validation-indicator.is-invalid").first();
  await expect(reactionIndicator).toHaveAttribute("title", "RXN SMARTS 无法解析");
  const reactionInputBox = await reactionInput.boundingBox();
  const reactionIndicatorBox = await reactionIndicator.boundingBox();
  expect(reactionInputBox).not.toBeNull();
  expect(reactionIndicatorBox).not.toBeNull();
  expect((reactionIndicatorBox?.x ?? 0)).toBeGreaterThan((reactionInputBox?.x ?? 0) + (reactionInputBox?.width ?? 0) - 30);
  await dialog.getByRole("button", { name: "关闭高级查询" }).click();
  const quickInput = page.getByRole("searchbox", { name: "反应 SMILES 快速查询" });
  await quickInput.fill("not a reaction");
  await expect(page.locator(".search-field .query-validation-indicator.is-invalid")).toHaveAttribute("title", "请输入“反应物>>产物”格式");
});

test("reaction advanced query builds a structured AND/OR/NOT expression", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1440, height: 960 });
  await page.goto("/");
  await page.getByRole("button", { name: "高级查询", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "高级查询" });
  await expect(dialog).toBeVisible();
  const dialogBox = await dialog.boundingBox();
  expect(dialogBox).not.toBeNull();
  expect(Math.abs((dialogBox?.x ?? 0) + (dialogBox?.width ?? 0) / 2 - 720)).toBeLessThan(20);

  const reactionEditor = dialog.locator('.reaction-editor-field[data-renderer="chemdoodle-reaction"]');
  const reactionCanvas = reactionEditor.frameLocator("iframe").locator("#editor-canvas");
  await expect(reactionCanvas).toBeVisible();
  const reactionTools = reactionEditor.frameLocator("iframe");
  const shapesButton = reactionTools.locator('[title="Shapes"]:visible').first();
  await expect(shapesButton).toBeVisible();
  await shapesButton.click();
  await reactionTools.locator('[title="More Shapes"]:visible').first().click();
  await expect(reactionTools.locator('[title="Synthetic Arrow"]:visible').first()).toBeVisible();
  const reactionSmilesInput = dialog.locator(".reaction-editor-field .topology-smiles-input input");
  await reactionSmilesInput.fill("C=C>>CC");
  await dialog.getByRole("button", { name: "添加条件" }).click();
  const conditionFields = dialog.locator('select[id^="reaction-advanced-query-field-"]');
  await conditionFields.nth(1).selectOption("minimum_activation_gibbs_free_energy_kcal_mol");
  await dialog.getByRole("spinbutton", { name: "最低活化自由能（kcal/mol）条件值" }).fill("37");
  await dialog.getByRole("checkbox", { name: "排除（NOT）" }).nth(1).check();
  await dialog.getByRole("button", { name: "添加条件" }).click();
  await conditionFields.nth(2).selectOption("reactant_smarts");
  await dialog.getByRole("textbox", { name: "前体 SMARTS条件值" }).fill("[C]=[C]");
  await dialog.getByRole("button", { name: "添加条件" }).click();
  await conditionFields.nth(3).selectOption("product_smarts");
  await dialog.getByRole("textbox", { name: "后体 SMARTS条件值" }).fill("[C][C]");
  await dialog.getByRole("combobox", { name: "反应高级查询逻辑运算" }).selectOption("or");
  await page.screenshot({ path: testInfo.outputPath("desktop-reaction-advanced-query.png"), fullPage: true });

  const responsePromise = page.waitForResponse((response) =>
    response.url().includes("/api/logical_reaction_query_service/list_logical_reactions")
    && response.request().method() === "POST"
    && response.request().postData()?.includes("filter_expression") === true,
  );
  await dialog.getByRole("button", { name: "应用高级查询" }).click();
  const response = await responsePromise;
  expect(response.status()).toBe(200);
  const request = response.request().postDataJSON() as { filter_expression?: string };
  const expression = JSON.parse(request.filter_expression ?? "{}") as {
    operator?: string;
    conditions?: Array<{ field: string; value: string | number; negated?: boolean }>;
  };
  expect(expression.operator).toBe("or");
  expect(expression.conditions).toEqual([
    { field: "rxn_smarts", value: "C=C>>CC" },
    { field: "minimum_activation_gibbs_free_energy_kcal_mol", value: 37, negated: true },
    { field: "reactant_smarts", value: "[C]=[C]" },
    { field: "product_smarts", value: "[C][C]" },
  ]);
  await expect(page.getByRole("heading", { name: "高级查询结果" })).toBeVisible();
  await expect(page.locator(".advanced-query-active")).toContainText("4 个条件");

  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole("button", { name: "高级查询", exact: true }).click();
  const mobileDialog = page.getByRole("dialog", { name: "高级查询" });
  const mobileDialogBox = await mobileDialog.boundingBox();
  expect(mobileDialogBox?.width ?? Infinity).toBeLessThanOrEqual(390);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("mobile-reaction-advanced-query.png"), fullPage: true });
});

test("catalog pagination jumps directly to a requested page", async ({ page }) => {
  await mockReactionCatalog(page, 24);
  await page.goto("/");
  await expect(page.locator(".reaction-path-card").first()).toBeVisible();
  await expect(page.getByLabel("跳转页码").first()).toHaveValue("1");

  await page.getByLabel("跳转页码").first().fill("2");
  const responsePromise = page.waitForResponse((response) =>
    response.url().includes("/api/logical-reactions")
    && response.url().includes("limit=12")
    && response.url().includes("offset=12"),
  );
  await page.getByRole("button", { name: "跳转" }).first().click();
  const response = await responsePromise;

  expect(response.status()).toBe(200);
  await expect(page.getByLabel("跳转页码").first()).toHaveValue("2");
  await expect(page.getByLabel("跳转页码").nth(1)).toHaveValue("2");
  await expect(page.locator(".catalog-pagination").first()).toContainText("13-24");
  await expect(page.locator(".catalog-pagination").nth(1)).toContainText("13-24");
});

test("mapped reaction shows every partial thermodynamic profile and level", async ({ page }, testInfo) => {
  const state = {
    topologies: [],
    enthalpy_hartree: -10,
    gibbs_free_energy_hartree: -11,
    entropy_cal_mol_k: 1,
  };
  await page.route("**/api/mapped-reactions/*/thermodynamics*", async (route) => {
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        mapped_reaction_id: "00000000-0000-7000-8000-000000000001",
        profiles: [
          {
            mapped_reaction_id: "00000000-0000-7000-8000-000000000001",
            policy_version: "mapped-reaction-thermodynamics-v1",
            electronic_level: ["DFT", "DFT", null, "wB97M-V", "Def2TZVPP"],
            thermochemistry_level: ["DFT", "DFT", null, "B3LYP-D3BJ", "Def2SVP"],
            level_of_theory: "B3LYP-D3BJ/Def2SVP//wB97M-V/Def2TZVPP",
            temperature_kelvin: 298.15,
            pressure_atm: 1,
            reactants: state,
            transition_state: null,
            products: { ...state },
            activation: null,
            reaction: { enthalpy_kcal_mol: -1, gibbs_free_energy_kcal_mol: -2, entropy_cal_mol_k: 3 },
          },
          {
            mapped_reaction_id: "00000000-0000-7000-8000-000000000001",
            policy_version: "mapped-reaction-thermodynamics-v1",
            electronic_level: ["DFT", "DFT", null, "B3LYP", "Def2SVP"],
            thermochemistry_level: ["DFT", "DFT", null, "B3LYP", "Def2SVP"],
            level_of_theory: "B3LYP/Def2SVP",
            temperature_kelvin: 298.15,
            pressure_atm: 1,
            reactants: state,
            transition_state: { ...state },
            products: null,
            activation: { enthalpy_kcal_mol: 4, gibbs_free_energy_kcal_mol: 5, entropy_cal_mol_k: 6 },
            reaction: null,
          },
        ],
      }),
    });
  });
  await page.route("**/api/mapped-reactions/*/energy-profile*", async (route) => {
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        mapped_reaction_id: "00000000-0000-7000-8000-000000000001",
        energy_kind: "gibbs_free_energy_hartree",
        reference_node_id: "00000000-0000-7000-8000-000000000101",
        points: [
          {
            node_id: "00000000-0000-7000-8000-000000000101",
            node_key: "reactants",
            node_index: 0,
            role: "reactant",
            energy_kind: "gibbs_free_energy_hartree",
            energy_hartree: -100,
            relative_energy_kcal_mol: 0,
          },
          {
            node_id: "00000000-0000-7000-8000-000000000102",
            node_key: "transition-state",
            node_index: 1,
            role: "transition_state",
            energy_kind: "gibbs_free_energy_hartree",
            energy_hartree: -99.97,
            relative_energy_kcal_mol: 18.825284,
          },
          {
            node_id: "00000000-0000-7000-8000-000000000103",
            node_key: "products",
            node_index: 2,
            role: "product",
            energy_kind: "gibbs_free_energy_hartree",
            energy_hartree: -100.006,
            relative_energy_kcal_mol: -3.765057,
          },
        ],
        edges: [{
          edge_id: "00000000-0000-7000-8000-000000000104",
          edge_key: "elementary-step",
          source_node_id: "00000000-0000-7000-8000-000000000101",
          target_node_id: "00000000-0000-7000-8000-000000000103",
          transition_state_node_id: "00000000-0000-7000-8000-000000000102",
          reaction_energy_kcal_mol: -3.765057,
          forward_barrier_kcal_mol: 18.825284,
          reverse_barrier_kcal_mol: 22.590341,
        }],
      }),
    });
  });

  await page.goto("/");
  const firstReactionGroup = page.locator(".reaction-card-group").first();
  await expect(firstReactionGroup.locator(".reaction-path-card")).toBeVisible();
  await firstReactionGroup.locator(".reaction-path-card").click();
  const expansion = firstReactionGroup.locator(".mapped-reaction-expansion");
  await expect(expansion.locator(".thermo-profile")).toHaveCount(2);
  await expect(expansion.locator(".thermo-level").nth(0)).toHaveText("B3LYP-D3BJ/Def2SVP//wB97M-V/Def2TZVPP");
  await expect(expansion.locator(".thermo-level").nth(1)).toHaveText("B3LYP/Def2SVP");
  await expect(expansion.locator(".thermo-profile").nth(0).locator(".thermo-metrics > div")).toHaveCount(3);
  await expect(expansion.locator(".thermo-profile").nth(1).locator(".thermo-metrics > div")).toHaveCount(3);
  await expect(expansion.locator(".thermo-profile").nth(0)).toContainText("ΔG 反应");
  await expect(expansion.locator(".thermo-profile").nth(0)).not.toContainText("ΔG‡");
  await expect(expansion.locator(".thermo-profile").nth(1)).toContainText("ΔG‡");
  await expect(expansion.locator(".thermo-profile").nth(1)).not.toContainText("ΔG 反应");
  const potentialEnergy = expansion.locator(".reaction-potential-energy");
  await expect(potentialEnergy).toBeVisible();
  await expect(potentialEnergy.locator(".potential-stage-label")).toHaveText(["前体", "TS", "后体"]);
  await expect(potentialEnergy.locator(".potential-relative-label")).toHaveText(["+0.00 kcal/mol", "+18.83 kcal/mol", "-3.77 kcal/mol"]);
  await expect(potentialEnergy.locator(".potential-absolute-label")).toHaveText(["G -100.0000 Eh", "G -99.9700 Eh", "G -100.0060 Eh"]);
  await expect(potentialEnergy.locator(".reaction-potential-energy-metrics")).toContainText("18.83");
  await expect(potentialEnergy.locator(".reaction-potential-energy-metrics")).toContainText("22.59");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(potentialEnergy).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("mobile-reaction-potential-energy.png"), fullPage: true });
});

test("V2000 M CHG records survive ChemDoodle rendering", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 960 });
  const target = await findLogicalReactionPage(page, "DA bench");
  const topologyId = target.reaction.reactant_topology_ids[0];
  if (!topologyId) throw new Error("DA fixture is missing its first reactant topology");
  const molfileResponse = await page.request.get(`/api/depictions/topology/${topologyId}.mol`);
  expect(molfileResponse.ok()).toBe(true);
  const molfile = await molfileResponse.text();
  expect(molfile).toContain("V2000");
  const chargedMolfile = molfile.replace("M  END", "M  CHG  2   1  -1   2   1\nM  END");
  expect(chargedMolfile).not.toBe(molfile);
  await page.route(`**/api/depictions/topology/${topologyId}.mol`, async (route) => {
    await route.fulfill({
      contentType: "chemical/x-mdl-molfile",
      body: chargedMolfile,
    });
  });
  await page.goto("/");
  await expect(page.locator(".reaction-path-card").first()).toBeVisible();
  if (target.pageNumber > 1) {
    const targetOffset = (target.pageNumber - 1) * 12;
    const responsePromise = page.waitForResponse((response) =>
      response.url().includes("/api/logical-reactions")
      && response.url().includes(`offset=${targetOffset}`),
    );
    await page.getByLabel("跳转页码").first().fill(String(target.pageNumber));
    await page.getByRole("button", { name: "跳转" }).first().click();
    expect((await responsePromise).status()).toBe(200);
  }

  const reactionGroup = page.locator(".reaction-card-group").filter({
    hasText: "DA bench",
  });
  const reactionCard = reactionGroup.locator(".reaction-path-card");
  await expect(reactionCard).toBeVisible();
  await reactionCard.scrollIntoViewIfNeeded();
  await expect(reactionCard.locator('[data-formal-charges="-1,1"]')).toHaveCount(1);

  await reactionCard.click();
  const expansion = reactionGroup.locator(".mapped-reaction-expansion");
  await expect(expansion).toBeVisible();
  await expect(expansion.locator('[data-formal-charges="-1,1"]')).not.toHaveCount(0);
});

test("geometry cards preserve drawer preview and link to a standalone detail page", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1440, height: 960 });
  const defaultGeometryRequest = page.waitForRequest((request) =>
    request.url().includes("/api/geometry_query_service/list_geometries")
    && request.method() === "POST"
    && request.postData()?.includes('"thermodynamic_only":true') === true,
  );
  await page.goto("/geometries");
  await defaultGeometryRequest;
  await expect(page.getByRole("checkbox", { name: "仅显示含有热力学属性的几何" })).toBeChecked();
  await expect(page).toHaveURL(/\/geometries(?:\?|$)/);
  await expect(page.locator("#geometry-view-title")).toBeVisible();
  await expect(page.locator(".metrics-band > div")).toHaveCount(5);
  await expect(page.getByRole("region", { name: "当前项目数据库概览" })).toContainText("几何构象总数");
  await expect(page.locator(".geometry-card").first()).toBeVisible();
  await expect(page.locator(".geometry-card").first()).toHaveAttribute(
    "data-imaginary-frequency-status",
    /present|absent|unavailable/,
  );
  await expect(page.locator(".geometry-card").first().locator(".geometry-frequency-status")).toHaveText(
    /含虚频|无虚频|未提供频率/,
  );

  await page.locator(".geometry-card").first().click();
  await expect(page).toHaveURL(/\/geometries\?.*preview_geometry=/);
  await expect(page.locator("#geometry-detail-title")).toBeVisible();
  await expect(page.locator(".geometry-detail-drawer .geometry-canvas-3d canvas")).toHaveCount(1);
  await expect(page.locator(".geometry-detail-drawer .molecule-state")).toHaveCount(0);
  const detailLink = page.locator('.geometry-detail-drawer .drawer-header a[aria-label="在独立页面打开几何构象"]');
  await expect(detailLink).toHaveAttribute("href", /\/geometries\/[^?]+\?/);
  await detailLink.click();
  await expect(page.getByRole("heading", { name: "几何构象详情" })).toBeVisible();
  await expect(page.locator(".geometry-detail-page-content .geometry-canvas-3d")).toHaveAttribute("data-webgl-state", "ready", { timeout: 20_000 });
  const geometryRenderer = page.locator(".geometry-detail-page-content .geometry-canvas-3d");
  expect(await webGlCanvasHasDrawing(geometryRenderer)).toBe(true);
  const geometryCanvas = geometryRenderer.locator("canvas");
  expect(await dragChangesWebGlCanvas(page, geometryCanvas)).toBe(true);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("desktop-geometry-detail.png"), fullPage: true });
});

test("geometry quick query submits only the basic SMILES filter", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 960 });
  await page.goto("/geometries");
  const sidebarLayout = await page.locator(".geometry-filter-sidebar").evaluate((sidebar) => {
    const actions = sidebar.querySelector<HTMLElement>(".filter-actions");
    if (!actions) throw new Error("geometry filter actions are missing");
    const sidebarRect = sidebar.getBoundingClientRect();
    const actionsRect = actions.getBoundingClientRect();
    return { sidebarRight: sidebarRect.right, actionsRight: actionsRect.right };
  });
  expect(sidebarLayout.actionsRight).toBeLessThanOrEqual(sidebarLayout.sidebarRight + 1);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);

  const responsePromise = page.waitForResponse((response) =>
    response.url().includes("/api/geometry_query_service/list_geometries")
    && response.request().method() === "POST"
    && response.request().postData()?.includes('"topology_smiles":"C=C"') === true,
  );
  await page.getByRole("searchbox", { name: "SMILES 快速查询" }).fill("C=C");
  await page.getByRole("button", { name: "查询", exact: true }).click();
  const response = await responsePromise;
  const payload = response.request().postDataJSON() as {
    topology_smiles?: string;
    topology_smarts?: string | null;
    topology_mol_block?: string | null;
    thermodynamic_only?: boolean;
    filter_expression?: string | null;
  };
  const result = await response.json() as {
    items: Array<{ canonical_isomeric_smiles: string }>;
    page: { total: number };
  };
  expect(response.status()).toBe(200);
  expect(payload.topology_smiles).toBe("C=C");
  expect(payload.topology_smarts).toBeNull();
  expect(payload.topology_mol_block).toBeNull();
  expect(payload.thermodynamic_only).toBe(true);
  expect(payload.filter_expression).toBeNull();
  expect(result.page.total).toBeGreaterThan(0);
  await expect(page.getByRole("heading", { name: "SMILES 查询结果" })).toBeVisible();
});

test("geometry structure filters validate while editing and expose help", async ({ page }) => {
  await page.goto("/geometries");
  const quickInput = page.getByRole("searchbox", { name: "SMILES 快速查询" });
  await quickInput.fill("not smiles");
  await expect(page.locator(".geometry-filter-sidebar .query-validation-indicator.is-invalid")).toHaveAttribute("title", "SMILES 无法解析");
  await quickInput.fill("CCO");
  await expect(page.locator(".geometry-filter-sidebar .query-validation-indicator.is-valid")).toHaveAttribute("title", "SMILES 格式有效");

  await page.getByRole("button", { name: "高级查询", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "高级查询" });
  await dialog.locator('select[id^="advanced-query-field-"]').selectOption("topology_smarts");
  await dialog.getByRole("textbox", { name: "拓扑 SMARTS条件值" }).fill("[invalid");
  await expect(dialog.locator(".query-validation-indicator.is-invalid")).toHaveAttribute("title", "SMARTS 无法解析");
  await expect(dialog.getByRole("link", { name: "查看几何构象查询帮助" })).toBeVisible();
  await dialog.getByRole("button", { name: "关闭高级查询" }).click();

  await page.getByRole("link", { name: "几何构象查询帮助", exact: true }).click();
  await expect(page).toHaveURL(/\/help\/geometry-query(?:\?|$)/);
  await expect(page.getByRole("heading", { name: "几何构象查询帮助" })).toBeVisible();
  await expect(page.getByText("topology_smarts", { exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});

test("geometry advanced query builds AND, OR, and NOT expressions", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1440, height: 960 });
  await page.goto("/geometries");
  await page.getByRole("button", { name: "高级查询", exact: true }).click();

  const dialog = page.getByRole("dialog", { name: "高级查询" });
  await expect(dialog).toBeVisible();
  const dialogBox = await dialog.boundingBox();
  expect(dialogBox).not.toBeNull();
  expect(Math.abs((dialogBox?.x ?? 0) + (dialogBox?.width ?? 0) / 2 - 720)).toBeLessThan(20);
  await expect(dialog.locator('[data-query-field="topology_smiles"] .topology-editor')).toBeVisible();
  await dialog.locator('[data-query-field="topology_smiles"] .topology-smiles-input input').fill("C=C");

  await dialog.getByRole("button", { name: "添加条件" }).click();
  const conditionFields = dialog.locator('select[id^="advanced-query-field-"]');
  await conditionFields.nth(1).selectOption("imaginary_frequency_status");
  await dialog.getByRole("combobox", { name: "虚频状态条件" }).selectOption("unavailable");
  await dialog.getByRole("checkbox", { name: "排除（NOT）" }).nth(1).check();
  await dialog.getByRole("combobox", { name: "高级查询逻辑运算" }).selectOption("or");
  await page.screenshot({ path: testInfo.outputPath("desktop-geometry-advanced-query.png"), fullPage: true });

  const responsePromise = page.waitForResponse((response) =>
    response.url().includes("/api/geometry_query_service/list_geometries")
    && response.request().method() === "POST"
    && response.request().postData()?.includes("filter_expression") === true,
  );
  await dialog.getByRole("button", { name: "应用高级查询" }).click();
  const response = await responsePromise;
  expect(response.status()).toBe(200);
  const payload = response.request().postDataJSON() as { filter_expression?: string };
  const expression = JSON.parse(payload.filter_expression ?? "{}") as {
    operator?: string;
    conditions?: Array<{ field: string; value: string; negated?: boolean }>;
  };
  expect(expression.operator).toBe("or");
  expect(expression.conditions).toEqual([
    { field: "topology_smiles", value: "C=C" },
    { field: "imaginary_frequency_status", value: "unavailable", negated: true },
  ]);
  await expect(page.getByRole("heading", { name: "高级查询结果" })).toBeVisible();
  await expect(page.locator(".advanced-query-active")).toContainText("2 个条件");

  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole("button", { name: "高级查询", exact: true }).click();
  const mobileDialog = page.getByRole("dialog", { name: "高级查询" });
  const mobileDialogBox = await mobileDialog.boundingBox();
  expect(mobileDialogBox?.width ?? Infinity).toBeLessThanOrEqual(390);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("mobile-geometry-advanced-query.png"), fullPage: true });
});

test("ChemDoodle editor keeps its SMILES display synchronized", async ({ page }) => {
  await page.goto("/geometries");
  await page.getByRole("button", { name: "高级查询", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "高级查询" });
  const sketcher = dialog.locator('.topology-editor-field[data-renderer="chemdoodle-sketcher"]').first();
  const editor = sketcher.frameLocator('iframe[title="ChemDoodle structure editor"]');
  const canvas = editor.locator("#editor-canvas");
  await expect(canvas).toBeVisible();
  await expect(editor.locator('[title="Single Bond"]').first()).toBeVisible();
  const canvasFingerprint = (): Promise<string> => canvas.evaluate((element) => {
    const context = (element as HTMLCanvasElement).getContext("2d");
    if (!context) throw new Error("editor canvas context is unavailable");
    const pixels = context.getImageData(0, 0, element.width, element.height).data;
    let hash = 0;
    for (let index = 0; index < pixels.length; index += 97) hash = (hash * 31 + pixels[index]) >>> 0;
    return String(hash);
  });
  const emptyCanvasFingerprint = await canvasFingerprint();
  const smilesInput = sketcher.locator('.topology-smiles-input input');
  await smilesInput.fill("CCO");
  await expect(smilesInput).toHaveValue("CCO");
  await expect.poll(canvasFingerprint).not.toBe(emptyCanvasFingerprint);
  const molfile = [
    "sync-check",
    "  TriCycle",
    "",
    "  2  0  0  0  0  0            999 V2000",
    "    0.0000    0.0000    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0",
    "    1.4000    0.0000    0.0000 O   0  0  0  0  0  0  0  0  0  0  0  0",
    "M  END",
    "",
  ].join("\n");
  await sketcher.locator("iframe").evaluate((frame, value) => {
    frame.contentWindow?.postMessage({
      protocol: "tricycle-chemdoodle-editor/1",
      type: "command",
      requestId: 1,
      command: "loadMolfile",
      molfile: value,
    }, "*");
  }, molfile);
  await expect(smilesInput).toHaveValue("C.O");
  const layout = await sketcher.evaluate((element) => {
    const frame = element.querySelector(".topology-editor");
    const smiles = element.querySelector(".topology-smiles-input");
    if (!frame || !smiles) throw new Error("editor layout elements are missing");
    const frameRect = frame.getBoundingClientRect();
    const smilesRect = smiles.getBoundingClientRect();
    return {
      frameBottom: frameRect.bottom,
      frameWidth: frameRect.width,
      smilesTop: smilesRect.top,
      smilesWidth: smilesRect.width,
    };
  });
  expect(layout.smilesTop - layout.frameBottom).toBeGreaterThanOrEqual(12);
  expect(Math.abs(layout.smilesWidth - layout.frameWidth)).toBeLessThanOrEqual(1);
  const canvasLayout = await editor.locator("#editor-canvas").evaluate((canvas) => {
    const rect = canvas.getBoundingClientRect();
    const body = canvas.ownerDocument.body;
    return { top: rect.top, bottom: rect.bottom, bodyHeight: body.clientHeight };
  });
  expect(canvasLayout.top).toBeGreaterThanOrEqual(0);
  expect(canvasLayout.bottom).toBeLessThanOrEqual(canvasLayout.bodyHeight + 1);
});

test("geometry advanced query provides the ChemDoodle editor and SMARTS filter", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 960 });
  await page.goto("/geometries");
  await page.getByRole("button", { name: "高级查询", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "高级查询" });

  const sketcher = dialog.locator('.topology-editor-field[data-renderer="chemdoodle-sketcher"]');
  const editor = sketcher.frameLocator('iframe[title="ChemDoodle structure editor"]');
  const canvas = editor.locator("#editor-canvas");
  await expect(canvas).toBeVisible();
  await expect(editor.locator('[title="Single Bond"]').first()).toBeVisible();
  expect(await page.evaluate(() => [typeof window.$, typeof window.jQuery])).toEqual([
    "undefined",
    "undefined",
  ]);
  expect(await editor.locator("body").evaluate(() => [typeof window.$, typeof window.jQuery])).toEqual([
    "undefined",
    "undefined",
  ]);
  const editorResources = await editor.locator("body").evaluate(() =>
    performance.getEntriesByType("resource").map((entry) => entry.name.toLowerCase()),
  );
  expect(editorResources.some((resource) => resource.includes("jquery"))).toBe(false);

  await sketcher.locator('.topology-smiles-input input').fill("C=C");
  const responsePromise = page.waitForResponse((response) =>
    response.url().includes("/api/geometry_query_service/list_geometries") &&
    response.request().method() === "POST",
  );
  await dialog.getByRole("button", { name: "应用高级查询" }).click();

  const response = await responsePromise;
  const payload = response.request().postDataJSON() as { filter_expression?: string };
  expect(response.status()).toBe(200);
  const expression = JSON.parse(payload.filter_expression ?? "{}") as {
    operator?: string;
    conditions?: Array<{ field: string; value: string }>;
  };
  expect(expression.operator).toBe("and");
  expect(expression.conditions?.[0]?.field).toBe("topology_smiles");
  expect(expression.conditions?.[0]?.value).toBeTruthy();
  const molResult = await response.json() as { page: { total: number } };
  expect(molResult.page.total).toBeGreaterThan(0);
  await expect(page.locator(".filter-result-count strong")).toHaveText(String(molResult.page.total));

  await page.getByRole("button", { name: "高级查询", exact: true }).click();
  const smartsDialog = page.getByRole("dialog", { name: "高级查询" });
  await smartsDialog.locator('select[id^="advanced-query-field-"]').selectOption("topology_smarts");
  const smartsResponsePromise = page.waitForResponse((candidate) =>
    candidate.url().includes("/api/geometry_query_service/list_geometries") &&
    candidate.request().method() === "POST" &&
    candidate.request().postData()?.includes("filter_expression") === true,
  );
  await smartsDialog.getByRole("textbox", { name: "SMARTS条件值" }).fill("[s]");
  await smartsDialog.getByRole("button", { name: "应用高级查询" }).click();
  const smartsResponse = await smartsResponsePromise;
  const smartsPayload = smartsResponse.request().postDataJSON() as { filter_expression?: string };
  const smartsExpression = JSON.parse(smartsPayload.filter_expression ?? "{}") as {
    conditions?: Array<{ field: string; value: string }>;
  };
  expect(smartsResponse.status()).toBe(200);
  expect(smartsExpression.conditions).toEqual([{ field: "topology_smarts", value: "[s]" }]);
  const smartsResult = await smartsResponse.json() as { page: { total: number } };
  expect(smartsResult.page.total).toBeGreaterThan(0);
  await expect(page.locator(".filter-result-count strong")).toHaveText(String(smartsResult.page.total));
});

test("reaction advanced query accepts a multicomponent MOL Block", async ({ page }) => {
  await page.goto("/reactions");
  await page.getByRole("button", { name: "高级查询", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "高级查询" });
  await dialog.locator('select[id^="reaction-advanced-query-field-"]').selectOption("reactant_mol_block");
  const reactantInput = dialog.locator('[data-query-field="reactant_mol_block"] .topology-smiles-input input');
  await reactantInput.fill("C.O");
  await expect(reactantInput).toHaveValue("C.O");
  await expect(dialog.locator('[data-query-field="reactant_mol_block"]')).toHaveAttribute(
    "data-validation-status",
    "valid",
  );
  await dialog.getByRole("button", { name: "添加条件" }).click();
  await dialog.locator('select[id^="reaction-advanced-query-field-"]').nth(1).selectOption("product_mol_block");
  const productInput = dialog.locator('[data-query-field="product_mol_block"] .topology-smiles-input input');
  await productInput.fill("CO");
  await expect(productInput).toHaveValue("CO");
  await expect(dialog.locator('[data-query-field="product_mol_block"]')).toHaveAttribute(
    "data-validation-status",
    "valid",
  );

  const responsePromise = page.waitForResponse((response) =>
    response.url().includes("/api/logical_reaction_query_service/list_logical_reactions")
    && response.request().method() === "POST"
    && response.request().postData()?.includes("filter_expression") === true,
  );
  await dialog.getByRole("button", { name: "应用高级查询" }).click();
  const response = await responsePromise;
  expect(response.status()).toBe(200);
  const request = response.request().postDataJSON() as { filter_expression?: string };
  const expression = JSON.parse(request.filter_expression ?? "{}") as {
    conditions?: Array<{ field: string; value: string }>;
  };
  expect(expression.conditions?.map((condition) => condition.field)).toEqual([
    "reactant_mol_block",
    "product_mol_block",
  ]);
  expect(expression.conditions?.[0]?.value).toContain("  2  0");
  expect(expression.conditions?.[1]?.value).toContain("  2  1");
});

test("sandbox editor reports a failed resource and recovers on retry", async ({ page }) => {
  let rejectEditorScript = true;
  await page.route("**/editor/chemdoodle-editor.js", async (route) => {
    if (rejectEditorScript) {
      rejectEditorScript = false;
      await route.fulfill({ status: 503, contentType: "text/plain", body: "temporary failure" });
      return;
    }
    await route.continue();
  });

  await page.goto("/geometries");
  await page.getByRole("button", { name: "高级查询", exact: true }).click();
  const editorContainer = page.locator('.topology-editor-field[data-renderer="chemdoodle-sketcher"]');
  await expect(editorContainer.getByRole("button", { name: "重新加载编辑器" })).toBeVisible();
  await editorContainer.getByRole("button", { name: "重新加载编辑器" }).click();
  await expect(
    editorContainer.frameLocator('iframe[title="ChemDoodle structure editor"]').locator('[title="Single Bond"]').first(),
  ).toBeVisible();
  await expect(editorContainer.locator(".topology-editor-state")).toHaveCount(0);
});

test("non-chemistry routes do not download the ChemDoodle runtime", async ({ page }) => {
  await page.goto("/login");
  await expect(page.getByRole("heading", { name: "登录" })).toBeVisible();
  const resources = await page.evaluate(() =>
    performance.getEntriesByType("resource").map((entry) => entry.name.toLowerCase()),
  );
  expect(resources.some((resource) => resource.includes("/vendor/chemdoodle"))).toBe(false);
  expect(resources.some((resource) => resource.includes("chemdoodleweb-global.js"))).toBe(false);
  expect(await page.evaluate(() => typeof window.ChemDoodle)).toBe("undefined");
});

test("artifact catalog uses cursor paging and keeps a previous-page cursor stack", async ({ page }) => {
  const projectId = "00000000-0000-7000-8000-000000000201";
  const artifact = (index: number) => ({
    id: `00000000-0000-7000-8000-${String(index).padStart(12, "0")}`,
    original_filename: `cursor-page-${index}.log`,
    size_bytes: index,
    content_sha256: String(index).repeat(64),
    visibility: "project",
    artifact_kind: "calculation_output",
    storage_status: "available",
    project_id: projectId,
    created_by_user_id: "00000000-0000-7000-8000-000000000002",
    media_type: "text/plain",
    storage_verified_at: "2026-08-16T00:00:00Z",
    preview_available: true,
  });
  const observedCursors: string[] = [];
  await page.route("**/api/artifacts?*", async (route) => {
    const url = new URL(route.request().url());
    if (!url.searchParams.has("cursor")) {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({ items: [artifact(1)], page: { total: 2, limit: 1, offset: 0 } }),
      });
      return;
    }
    const cursor = url.searchParams.get("cursor") ?? "";
    observedCursors.push(cursor);
    const secondPage = cursor === "second-page";
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        items: [artifact(secondPage ? 2 : 1)],
        page: {
          total: -1,
          limit: 50,
          offset: secondPage ? 50 : 0,
          next_cursor: secondPage ? null : "second-page",
        },
      }),
    });
  });

  await page.goto("/artifacts");
  await expect(page.getByText("cursor-page-1.log")).toBeVisible();
  await expect(page.getByLabel("跳转页码")).toHaveCount(0);
  expect(observedCursors[0]).toBe("");

  await page.getByRole("button", { name: "下一页" }).first().click();
  await expect(page.getByText("cursor-page-2.log")).toBeVisible();
  expect(observedCursors).toContain("second-page");

  await page.getByRole("button", { name: "上一页" }).first().click();
  await expect(page.getByText("cursor-page-1.log")).toBeVisible();
  expect(observedCursors.filter((cursor) => cursor === "")).toHaveLength(2);
});

test("geometry catalog scrolling uses static SVG without WebGL contexts", async ({ page }) => {
  const contextWarnings: string[] = [];
  page.on("console", (message) => {
    if (message.text().includes("Too many active WebGL contexts")) contextWarnings.push(message.text());
  });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/geometries");
  await expect(page.locator(".geometry-card").first()).toBeVisible();

  const scrollHeight = await page.evaluate(() => document.documentElement.scrollHeight);
  const positions = Array.from({ length: Math.ceil(scrollHeight / 500) + 1 }, (_, index) => index * 500);
  for (const position of [...positions, ...[...positions].reverse()]) {
    await page.evaluate((top) => window.scrollTo(0, top), position);
    await page.waitForTimeout(80);
  }

  await page.evaluate(() => window.scrollTo(0, 0));
  await expect(page.locator(".geometry-card").first().locator('[data-renderer="rdkit-dof"] img')).toBeVisible();
  await expect(page.locator(".geometry-card").first().locator(".molecule-state")).toHaveCount(0);
  await expect(page.locator('.geometry-card [data-renderer="chemdoodle-transform-3d"]')).toHaveCount(0);
  expect(contextWarnings).toEqual([]);
});

test("authenticated artifact upload controls fit desktop and mobile", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1440, height: 960 });
  await page.goto("/artifacts");
  await expect(page.getByRole("heading", { name: "原始文件" })).toBeVisible();
  await page.getByRole("link", { name: "批量上传" }).click();
  await expect(page.getByRole("heading", { name: "批量文件上传" })).toBeVisible();
  await expect(page.getByRole("button", { name: "选择文件", exact: true })).toBeVisible();
  await expect(page.getByRole("combobox", { name: "上传文件类型" })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("desktop-artifact-upload.png"), fullPage: true });

  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByRole("button", { name: "选择文件", exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("mobile-artifact-upload.png"), fullPage: true });
});

test("upload file and folder selections append to one queue", async ({ page }) => {
  await page.goto("/uploads");
  const inputs = page.locator('.upload-source-actions input[type="file"]');

  await inputs.nth(0).evaluate((element) => {
    const transfer = new DataTransfer();
    transfer.items.add(new File(["file"], "single.log", { type: "text/plain" }));
    const input = element as HTMLInputElement;
    input.files = transfer.files;
    input.dispatchEvent(new Event("change", { bubbles: true }));
  });
  await inputs.nth(1).evaluate((element) => {
    const transfer = new DataTransfer();
    for (const path of ["folder-a/a.log", "folder-b/b.log"]) {
      const filename = path.slice(path.lastIndexOf("/") + 1);
      const file = new File([path], filename, { type: "text/plain" });
      Object.defineProperty(file, "webkitRelativePath", { value: path });
      transfer.items.add(file);
    }
    const input = element as HTMLInputElement;
    input.files = transfer.files;
    input.dispatchEvent(new Event("change", { bubbles: true }));
  });

  await expect(page.locator(".upload-source header")).toContainText("3 个");
  await expect(page.locator(".upload-task-list")).toContainText("folder-a/a.log");
  await expect(page.locator(".upload-task-list")).toContainText("folder-b/b.log");
});

test("dropping multiple folders recursively adds every file", async ({ page }) => {
  await page.goto("/uploads");
  await page.locator(".upload-dropzone").evaluate((zone) => {
    const fileEntry = (name: string, content: string) => ({
      isFile: true,
      isDirectory: false,
      name,
      file: (success: (file: File) => void) => success(new File([content], name, { type: "text/plain" })),
    });
    const directoryEntry = (name: string, children: unknown[]) => {
      let firstRead = true;
      return {
        isFile: false,
        isDirectory: true,
        name,
        createReader: () => ({
          readEntries: (success: (entries: unknown[]) => void) => {
            success(firstRead ? children : []);
            firstRead = false;
          },
        }),
      };
    };
    const transfer = new DataTransfer();
    const entries = [
      directoryEntry("folder-a", [fileEntry("a.log", "a")]),
      directoryEntry("folder-b", [
        fileEntry("b.log", "b"),
        directoryEntry("nested", [fileEntry("c.log", "c")]),
      ]),
    ];
    entries.forEach((entry, index) => {
      transfer.items.add(new File([String(index)], `folder-${index}.placeholder`));
      Object.defineProperty(transfer.items[index], "webkitGetAsEntry", { value: () => entry });
    });
    const event = new DragEvent("drop", { bubbles: true, cancelable: true, dataTransfer: transfer });
    zone.dispatchEvent(event);
  });

  await expect(page.locator(".upload-source header")).toContainText("3 个");
  await expect(page.locator(".upload-task-list")).toContainText("folder-a/a.log");
  await expect(page.locator(".upload-task-list")).toContainText("folder-b/b.log");
  await expect(page.locator(".upload-task-list")).toContainText("folder-b/nested/c.log");
});

test("expanded artifact shows an equal-height all-frame 3D movie", async ({ page }, testInfo) => {
  const runtimeErrors: string[] = [];
  page.on("pageerror", (error) => runtimeErrors.push(error.message));
  page.on("console", (message) => {
    if (message.type() === "error") runtimeErrors.push(message.text());
  });

  await page.setViewportSize({ width: 1440, height: 960 });
  const artifact = await findArtifactWithMultipleFrames(page);
  await page.goto(`/artifacts?project_id=00000000-0000-7000-8000-000000000201&artifact_id=${artifact.id}`);
  const firstArtifact = page.locator(".artifact-name-button").first();
  await expect(firstArtifact).toBeVisible();
  await firstArtifact.click();

  const listPane = page.locator(".artifact-frame-list-pane");
  const moviePane = page.locator('[data-renderer="chemdoodle-movie-3d"]');
  await expect(listPane).toBeVisible();
  await expect(moviePane).toBeVisible();
  await expect(moviePane.locator(".molecule-state")).toHaveCount(0, { timeout: 20_000 });
  const frameCount = await listPane.locator(".frame-list-row").count();
  expect(frameCount).toBeGreaterThan(1);
  await expect(moviePane).toHaveAttribute("data-frame-count", String(frameCount));

  const [listBox, movieBox] = await Promise.all([listPane.boundingBox(), moviePane.boundingBox()]);
  expect(listBox).not.toBeNull();
  expect(movieBox).not.toBeNull();
  expect(Math.abs((listBox?.height ?? 0) - (movieBox?.height ?? 0))).toBeLessThanOrEqual(1);
  expect(await allCanvasesHaveDrawing(page)).toBe(true);

  await moviePane.getByRole("button", { name: "跳到首帧" }).click();
  const movieCanvas = moviePane.locator("canvas");
  expect(await dragChangesWebGlCanvas(page, movieCanvas)).toBe(true);
  await moviePane.getByRole("button", { name: "下一帧" }).click();
  const pausedPosition = await moviePane.locator(".frame-movie-position").textContent();
  await expect(moviePane.getByRole("button", { name: "播放动画" })).toBeVisible();
  await forceWebGlRecovery(moviePane);
  await expect(moviePane.locator(".frame-movie-position")).toHaveText(pausedPosition ?? "");
  await expect(moviePane.getByRole("button", { name: "播放动画" })).toBeVisible();
  await moviePane.getByRole("button", { name: "播放动画" }).click();
  await expect(moviePane.getByRole("button", { name: "暂停动画" })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("desktop-artifact-frame-movie.png") });

  await page.setViewportSize({ width: 390, height: 844 });
  await moviePane.scrollIntoViewIfNeeded();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  const mobileMovieBox = await moviePane.boundingBox();
  expect(mobileMovieBox?.width ?? Infinity).toBeLessThanOrEqual(390);
  await page.screenshot({ path: testInfo.outputPath("mobile-artifact-frame-movie.png") });
  expect(runtimeErrors).toEqual([]);
});

test("selected artifact files use one MolOP batch request", async ({ page }) => {
  const batchId = "00000000-0000-7000-8000-000000000801";
  let batchRequests = 0;
  let completedUploads = 0;
  const manifest = new Map<string, { name: string; size: number }>();
  await page.route("**/api/upload-batches**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname === "/api/upload-batches" && request.method() === "GET") {
      await route.fulfill({ contentType: "application/json", body: JSON.stringify({ items: [], total: 0, limit: 8, offset: 0 }) });
      return;
    }
    if (url.pathname === "/api/upload-batches" && request.method() === "POST") {
      const body = request.postDataJSON() as { files: Array<{ client_file_id: string; original_filename: string; size_bytes: number }>; shared_metadata: Record<string, string> };
      expect(body.shared_metadata).toEqual({ campaign: "parallel-screen" });
      for (const file of body.files) manifest.set(file.client_file_id, { name: file.original_filename, size: file.size_bytes });
      await route.fulfill({ status: 201, contentType: "application/json", body: JSON.stringify(uploadBatchFixture(batchId, { total_count: 3, total_bytes: 30, shared_metadata: body.shared_metadata })) });
      return;
    }
    if (url.pathname === `/api/upload-batches/${batchId}` && request.method() === "GET") {
      await route.fulfill({ contentType: "application/json", body: JSON.stringify(uploadBatchFixture(batchId, { status: completedUploads === 3 ? "completed" : "active", succeeded_count: completedUploads, total_count: 3, total_bytes: 30 })) });
      return;
    }
    if (!url.pathname.endsWith(`/upload-batches/${batchId}/files`) || request.method() !== "POST") {
      await route.fallback();
      return;
    }
    batchRequests += 1;
    completedUploads = manifest.size;
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify([...manifest.entries()].map(([clientFileId, file]) =>
        uploadBatchItemFixture(clientFileId, file.name, { size_bytes: file.size }),
      )),
    });
  });

  await page.goto("/uploads");
  await page.getByRole("combobox", { name: "上传文件类型" }).selectOption("auxiliary");
  await page.getByLabel("元数据键").fill("campaign");
  await page.getByLabel("元数据值").fill("parallel-screen");
  await page.locator('.upload-source-actions input[type="file"]').first().setInputFiles(
    Array.from({ length: 3 }, (_, index) => ({
      name: `parallel-${index}.txt`,
      mimeType: "text/plain",
      buffer: Buffer.from(`parallel-${index}`),
    })),
  );

  await page.getByRole("button", { name: "开始上传" }).click();
  await expect(page.locator(".upload-progress-band")).toContainText("3完成");
  expect(batchRequests).toBe(1);
});

test("upload queue automatically retries a rate-limited file", async ({ page }) => {
  const batchId = "00000000-0000-7000-8000-000000000806";
  let clientFileId = "";
  let uploadAttempts = 0;
  await page.route("**/api/upload-batches**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname === "/api/upload-batches" && request.method() === "GET") {
      await route.fulfill({ contentType: "application/json", body: JSON.stringify({ items: [], total: 0, limit: 8, offset: 0 }) });
      return;
    }
    if (url.pathname === "/api/upload-batches" && request.method() === "POST") {
      const body = request.postDataJSON() as { files: Array<{ client_file_id: string }> };
      clientFileId = body.files[0].client_file_id;
      await route.fulfill({ status: 201, contentType: "application/json", body: JSON.stringify(uploadBatchFixture(batchId, { total_count: 1, total_bytes: 4 })) });
      return;
    }
    if (url.pathname === `/api/upload-batches/${batchId}` && request.method() === "GET") {
      await route.fulfill({ contentType: "application/json", body: JSON.stringify(uploadBatchFixture(batchId, { status: uploadAttempts === 2 ? "completed" : "active", total_count: 1, total_bytes: 4, succeeded_count: uploadAttempts === 2 ? 1 : 0 })) });
      return;
    }
    if (url.pathname.endsWith(`/upload-batches/${batchId}/files`) && request.method() === "POST") {
      uploadAttempts += 1;
      if (uploadAttempts === 1) {
        await route.fulfill({ status: 429, headers: { "retry-after": "0" }, contentType: "application/json", body: JSON.stringify({ detail: "rate limited" }) });
        return;
      }
      await route.fulfill({ contentType: "application/json", body: JSON.stringify([
        uploadBatchItemFixture(clientFileId, "retry-after.log", { size_bytes: 4, attempt_count: 1 }),
      ]) });
      return;
    }
    await route.fallback();
  });

  await page.goto("/uploads");
  await page.locator('.upload-source-actions input[type="file"]').first().setInputFiles({
    name: "retry-after.log",
    mimeType: "text/plain",
    buffer: Buffer.from("data"),
  });
  await page.getByRole("button", { name: "开始上传" }).click();
  const row = page.locator(".upload-task-row").filter({ hasText: "retry-after.log" });
  await expect(row).toHaveCount(0);
  await expect(page.locator(".upload-source header")).toContainText("0 个");
  expect(uploadAttempts).toBe(2);
});

test("opening an uploaded file keeps the remaining upload request alive", async ({ page }) => {
  const batchId = "00000000-0000-7000-8000-000000000807";
  const artifactId = "00000000-0000-7000-8000-000000000808";
  const manifest = new Map<string, { name: string; size: number }>();
  const statuses = new Map<string, "queued" | "succeeded">();
  let uploadRequestCount = 0;
  let uploadRequestFailures = 0;
  let releaseDelayedUpload!: () => void;
  const delayedUploadRelease = new Promise<void>((resolve) => {
    releaseDelayedUpload = resolve;
  });
  let delayedUploadStartedResolve!: () => void;
  const delayedUploadStarted = new Promise<void>((resolve) => {
    delayedUploadStartedResolve = resolve;
  });
  let delayedUploadCompletedResolve!: () => void;
  const delayedUploadCompleted = new Promise<void>((resolve) => {
    delayedUploadCompletedResolve = resolve;
  });
  page.on("requestfailed", (request) => {
    if (new URL(request.url()).pathname === `/api/upload-batches/${batchId}/files`) {
      uploadRequestFailures += 1;
    }
  });

  await page.route("**/api/upload-batches**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname === "/api/upload-batches" && request.method() === "GET") {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          items: manifest.size ? [uploadBatchFixture(batchId, {
            total_count: manifest.size,
            total_bytes: [...manifest.values()].reduce((sum, file) => sum + file.size, 0),
            succeeded_count: [...statuses.values()].filter((status) => status === "succeeded").length,
          })] : [],
          total: manifest.size ? 1 : 0,
          limit: 8,
          offset: 0,
        }),
      });
      return;
    }
    if (url.pathname === "/api/upload-batches" && request.method() === "POST") {
      const body = request.postDataJSON() as {
        files: Array<{ client_file_id: string; original_filename: string; size_bytes: number }>;
      };
      for (const file of body.files) {
        manifest.set(file.client_file_id, { name: file.original_filename, size: file.size_bytes });
        statuses.set(file.client_file_id, "queued");
      }
      await route.fulfill({
        status: 201,
        contentType: "application/json",
        body: JSON.stringify(uploadBatchFixture(batchId, {
          total_count: manifest.size,
          total_bytes: [...manifest.values()].reduce((sum, file) => sum + file.size, 0),
        })),
      });
      return;
    }
    if (url.pathname === `/api/upload-batches/${batchId}` && request.method() === "GET") {
      const succeeded = [...statuses.values()].filter((status) => status === "succeeded").length;
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify(uploadBatchFixture(batchId, {
          status: succeeded === manifest.size ? "completed" : "active",
          total_count: manifest.size,
          total_bytes: [...manifest.values()].reduce((sum, file) => sum + file.size, 0),
          succeeded_count: succeeded,
          uploading_count: succeeded === manifest.size ? 0 : manifest.size - succeeded,
        })),
      });
      return;
    }
    if (url.pathname === `/api/upload-batches/${batchId}/items` && request.method() === "GET") {
      const items = [...manifest.entries()].map(([clientFileId, file], position) =>
        uploadBatchItemFixture(clientFileId, file.name, {
          position,
          size_bytes: file.size,
          status: statuses.get(clientFileId),
          artifact_file_id: statuses.get(clientFileId) === "succeeded" ? artifactId : null,
        }),
      );
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({ items, total: items.length, limit: 100, offset: 0 }),
      });
      return;
    }
    if (url.pathname !== `/api/upload-batches/${batchId}/files` || request.method() !== "POST") {
      await route.fallback();
      return;
    }
    uploadRequestCount += 1;
    const requestBody = request.postDataBuffer()?.toString() ?? "";
    const selected = [...manifest.entries()].filter(([clientFileId]) => requestBody.includes(clientFileId));
    if (uploadRequestCount === 2) {
      delayedUploadStartedResolve();
      await delayedUploadRelease;
    }
    for (const [clientFileId] of selected) statuses.set(clientFileId, "succeeded");
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(selected.map(([clientFileId, file], position) =>
        uploadBatchItemFixture(clientFileId, file.name, {
          position,
          size_bytes: file.size,
          artifact_file_id: artifactId,
        }),
      )),
    });
    if (uploadRequestCount === 2) delayedUploadCompletedResolve();
  });

  await page.goto("/uploads");
  await page.getByRole("combobox", { name: "上传文件类型" }).selectOption("auxiliary");
  await page.getByRole("slider", { name: "上传并发数" }).fill("2");
  await page.locator('.upload-source-actions input[type="file"]').first().setInputFiles(
    Array.from({ length: 33 }, (_, index) => ({
      name: `detail-navigation-${String(index).padStart(2, "0")}.txt`,
      mimeType: "text/plain",
      buffer: Buffer.from(`file-${index}`),
    })),
  );
  await page.getByRole("button", { name: "开始上传" }).click();
  await delayedUploadStarted;
  const uploadedRow = page.locator(".upload-task-row.is-succeeded").first();
  await expect(uploadedRow).toBeVisible();
  await uploadedRow.getByRole("link", { name: "查看文件" }).click();
  await expect(page).toHaveURL(new RegExp(`/artifacts/${artifactId}`));

  releaseDelayedUpload();
  await delayedUploadCompleted;
  expect(uploadRequestFailures).toBe(0);
  await page.goto(`/uploads?batch=${batchId}`);
  await expect(page.locator(".upload-progress-band")).toContainText("33完成");
});

test("an interrupted upload batch can be returned to the waiting queue", async ({ page }) => {
  const batchId = "00000000-0000-7000-8000-000000000809";
  const clientFileId = "00000000-0000-7000-8000-000000000810";
  let recovered = false;
  await page.route("**/api/upload-batches**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const batch = uploadBatchFixture(batchId, {
      total_count: 1,
      total_bytes: 7,
      uploading_count: recovered ? 0 : 1,
    });
    if (url.pathname === "/api/upload-batches" && request.method() === "GET") {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({ items: [batch], total: 1, limit: 8, offset: 0 }),
      });
      return;
    }
    if (url.pathname === `/api/upload-batches/${batchId}` && request.method() === "GET") {
      await route.fulfill({ contentType: "application/json", body: JSON.stringify(batch) });
      return;
    }
    if (url.pathname === `/api/upload-batches/${batchId}/recover` && request.method() === "POST") {
      recovered = true;
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify(uploadBatchFixture(batchId, {
          total_count: 1,
          total_bytes: 7,
          uploading_count: 0,
        })),
      });
      return;
    }
    if (url.pathname === `/api/upload-batches/${batchId}/items` && request.method() === "GET") {
      const item = uploadBatchItemFixture(clientFileId, "interrupted.log", {
        size_bytes: 7,
        status: recovered ? "queued" : "uploading",
        artifact_file_id: null,
        attempt_count: 1,
      });
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({ items: [item], total: 1, limit: 100, offset: 0 }),
      });
      return;
    }
    await route.fallback();
  });

  await page.goto(`/uploads?batch=${batchId}`);
  await expect(page.locator(".upload-task-row").filter({ hasText: "interrupted.log" })).toContainText("上传中");
  await page.getByRole("button", { name: "恢复中断上传" }).click();
  await expect(page.getByRole("alert")).toContainText("已恢复到等待队列");
  await expect(page.locator(".upload-task-row").filter({ hasText: "interrupted.log" })).toContainText("等待");
  await expect(page.getByRole("button", { name: "重新选择文件", exact: true })).toBeVisible();
});

test("ten-thousand-file queue keeps the rendered list paginated", async ({ page }) => {
  await page.goto("/uploads");
  await page.locator('.upload-source-actions input[type="file"]').first().evaluate((element) => {
    const transfer = new DataTransfer();
    for (let index = 0; index < 10_000; index += 1) {
      transfer.items.add(new File(["x"], `queue-${String(index).padStart(5, "0")}.log`, { type: "text/plain" }));
    }
    const input = element as HTMLInputElement;
    input.files = transfer.files;
    input.dispatchEvent(new Event("change", { bubbles: true }));
  });

  await expect(page.locator(".upload-source header")).toContainText("10,000 个");
  await expect(page.locator(".upload-list-controls")).toContainText("10,000 项");
  await expect(page.locator(".upload-task-row")).toHaveCount(100);
  await page.getByRole("button", { name: "下一页" }).click();
  await expect(page.locator(".upload-task-row").first()).toContainText("queue-00100.log");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});

test("calculation catalog redirects while frame and topology IDs open stable detail pages", async ({ page }, testInfo) => {
  const projectId = "00000000-0000-7000-8000-000000000201";
  await page.goto("/calculations");
  await expect(page).toHaveURL(/\/artifacts/);
  await expect(page.getByRole("heading", { name: "原始文件" })).toBeVisible();
  await expect(page.getByRole("button", { name: "计算帧" })).toHaveCount(0);

  const framesResponse = await page.request.get(`/api/calculation-frames?limit=1&offset=0&project_id=${projectId}`);
  expect(framesResponse.ok()).toBe(true);
  const framesPayload = await framesResponse.json() as {
    items: Array<{ id: string; topology_id: string; geometry_id: string }>;
  };
  const frame = framesPayload.items[0];
  expect(frame).toBeTruthy();

  await page.setViewportSize({ width: 1440, height: 960 });
  await page.goto(`/calculations/${frame.id}?project_id=${projectId}`);
  await expect(page).toHaveURL(new RegExp(`/calculations/${frame.id}`));
  await expect(page.getByRole("heading", { name: "计算帧详情" })).toBeVisible();
  await expect(page.locator(".frame-detail-page-content .geometry-canvas-3d")).toHaveAttribute("data-webgl-state", "ready", { timeout: 20_000 });
  expect(await webGlCanvasHasDrawing(page.locator(".frame-detail-page-content .geometry-canvas-3d"))).toBe(true);
  await expect(page.getByRole("link", { name: "查看分子拓扑" })).toHaveAttribute("href", new RegExp(`/topologies/${frame.topology_id}`));
  await expect(page.getByRole("link", { name: "查看几何构象" })).toHaveAttribute("href", new RegExp(`/geometries/${frame.geometry_id}`));
  await page.screenshot({ path: testInfo.outputPath("desktop-calculation-detail.png"), fullPage: true });

  await page.getByRole("link", { name: "查看分子拓扑" }).click();
  await expect(page).toHaveURL(new RegExp(`/topologies/${frame.topology_id}`));
  await expect(page.getByRole("heading", { name: "分子拓扑" })).toBeVisible();
  await expect(page.locator(".topology-structure-panel .molecule-state")).toHaveCount(0);
  await expect(page.locator(".entity-count-band")).toContainText("几何构象");
  await expect(page.locator(".topology-geometry-grid .geometry-card").first()).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("desktop-topology-detail.png"), fullPage: true });

  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("mobile-topology-detail.png"), fullPage: true });
});

test("global search links redirect to geometry filters", async ({ page }) => {
  await page.goto("/search?search=C%3DC");
  await expect(page).toHaveURL(/\/geometries\?/);
  expect(new URL(page.url()).searchParams.get("search")).toBe("C=C");
  await expect(page.getByRole("heading", { name: "几何构象", exact: true })).toBeVisible();
  await expect(page.locator('.utility-nav a[title="结构搜索"]')).toHaveCount(0);
});

test("account and project routes derive access from the session", async ({ page }) => {
  const meResponse = await page.request.get("/api/auth/me");
  expect(meResponse.ok()).toBe(true);
  const me = (await meResponse.json()) as {
    display_name: string;
    identity: { subject: string };
    projects: Array<{ project_id: string; project_name: string; permissions: string[] }>;
  };
  expect(me.projects.length).toBeGreaterThan(0);
  const project = me.projects[0];

  await page.goto("/account");
  await expect(page.getByRole("heading", { name: "账户与访问" })).toBeVisible();
  await expect(page.locator(".account-card").getByRole("heading", { name: me.display_name })).toBeVisible();
  await expect(page.getByText(me.identity.subject, { exact: true })).toHaveCount(0);

  await page.goto(`/projects/${project.project_id}?project_id=${project.project_id}`);
  await expect(page.getByRole("heading", { name: project.project_name })).toBeVisible();

  const managedProject = me.projects.find((item) => item.permissions.includes("project:manage"));
  expect(managedProject).toBeTruthy();
  await page.goto(`/projects/${managedProject!.project_id}/members?project_id=${managedProject!.project_id}`);
  await expect(page.getByRole("heading", { name: "项目成员" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "项目审计" })).toBeVisible();

  await page.goto("/projects/00000000-0000-7000-8000-000000000999");
  await expect(page).toHaveURL(/\/projects\?forbidden=true/);
  await expect(page.getByRole("heading", { name: "项目", exact: true })).toBeVisible();
  await expect(page.getByText("HTTP 403", { exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "没有项目访问权限" })).toBeVisible();
});

test("NexusX transport entry points stay on the frontend origin", async ({ page }) => {
  await page.route("**/api/auth/mcp-tokens", async (route) => {
    if (route.request().method() === "POST") {
      await route.fulfill({
        status: 201,
        contentType: "application/json",
        body: JSON.stringify({
          token: {
            id: "00000000-0000-7000-8000-000000001201",
            name: "Cursor",
            created_at: "2026-08-19T00:00:00Z",
            expires_at: "2027-08-19T00:00:00Z",
            last_used_at: null,
          },
          access_token: "mcp_playwright-token",
        }),
      });
      return;
    }
    await route.fulfill({ contentType: "application/json", body: "[]" });
  });
  await page.goto("/nexusx");
  await expect(page.getByRole("heading", { name: "增强接口" })).toBeVisible();
  await expect(page.getByRole("link", { name: /打开入口/ })).toHaveCount(4);
  const endpoints = [
    ["Direct-list GraphQL", "/nexusx/graphql"],
    ["Paginated GraphQL", "/nexusx/paginated-graphql"],
    ["UseCase MCP", "/nexusx/mcp/"],
    ["Voyager", "/nexusx/voyager/"],
  ] as const;
  const frontendUrl = new URL(page.url());
  for (const [name, path] of endpoints) {
    const card = page.locator(".nexusx-endpoint-card").filter({ hasText: name });
    await expect(card).toBeVisible();
    await expect(card.getByText("用途", { exact: true })).toBeVisible();
    await expect(card.getByText("使用", { exact: true })).toBeVisible();
    await expect(card.getByText("返回", { exact: true })).toBeVisible();
    await expect(card.getByText("适用", { exact: true })).toBeVisible();
    const href = await card.getByRole("link", { name: /打开入口/ }).getAttribute("href");
    expect(href).toBeTruthy();
    const url = new URL(href!, page.url());
    expect(url.origin).toBe(frontendUrl.origin);
    expect(url.port).toBe(frontendUrl.port);
    expect(url.pathname).toBe(path);
    await expect(card.getByRole("link", { name: /打开入口/ })).toHaveAttribute("target", "_blank");
  }
  const graphqlPage = await page.request.get("/nexusx/graphql");
  expect(graphqlPage.ok()).toBe(true);
  expect(graphqlPage.headers()["content-type"]).toContain("text/html");
  expect(await graphqlPage.text()).toContain("Direct-list GraphQL - Example Research Platform");
  const graphqlSchema = await page.request.get("/nexusx/graphql/schema");
  expect(graphqlSchema.ok()).toBe(true);
  expect(await graphqlSchema.text()).toContain("GraphQLCatalogService");
  const paginatedGraphqlPage = await page.request.get("/nexusx/paginated-graphql");
  expect(paginatedGraphqlPage.ok()).toBe(true);
  expect(await paginatedGraphqlPage.text()).toContain("Paginated GraphQL - Example Research Platform");
  const paginatedGraphqlSchema = await page.request.get("/nexusx/paginated-graphql/schema");
  expect(paginatedGraphqlSchema.ok()).toBe(true);
  expect(await paginatedGraphqlSchema.text()).not.toContain("GraphQLCatalogService");
  const directCard = page.locator(".nexusx-endpoint-card").filter({ hasText: "Direct-list GraphQL" });
  const paginatedCard = page.locator(".nexusx-endpoint-card").filter({ hasText: "Paginated GraphQL" });
  const directBox = await directCard.boundingBox();
  const paginatedBox = await paginatedCard.boundingBox();
  expect(directBox).toBeTruthy();
  expect(paginatedBox).toBeTruthy();
  expect(Math.abs(directBox!.y - paginatedBox!.y)).toBeLessThan(1);
  expect(Math.abs(directBox!.height - paginatedBox!.height)).toBeLessThan(1);
  await expect(directCard.locator(".nexusx-graphql-snippet")).toContainText("GraphQLCatalogService");
  await expect(paginatedCard.locator(".nexusx-graphql-snippet")).toContainText("ArtifactQueryService");
  const mcpCard = page.locator(".nexusx-endpoint-card").filter({ hasText: "UseCase MCP" });
  await expect(mcpCard).toHaveCSS("grid-column-start", "1");
  await expect(mcpCard).toHaveCSS("grid-column-end", "-1");
  const voyagerCard = page.locator(".nexusx-endpoint-card").filter({ hasText: "Voyager" });
  const voyagerBox = await voyagerCard.boundingBox();
  expect(voyagerBox).toBeTruthy();
  expect(voyagerBox!.width).toBeLessThan(directBox!.width * 1.1);
  expect(Math.abs(voyagerBox!.height - directBox!.height)).toBeLessThan(1);
  const projectDocs = await page.request.get("/docs");
  expect(projectDocs.ok()).toBe(true);
  const mcpInfo = await page.request.get("/nexusx/mcp/");
  expect(mcpInfo.ok()).toBe(true);
  expect(await mcpInfo.json()).toMatchObject({
    service: "UseCase MCP",
    transport: "Streamable HTTP",
    method: "POST",
  });
  await expect(mcpCard.getByRole("tab")).toHaveCount(6);
  await mcpCard.getByRole("tab", { name: "Cursor" }).click();
  await mcpCard.getByRole("button", { name: "生成 Token" }).click();
  await expect(mcpCard.locator(".nexusx-mcp-token-value")).toHaveText("mcp_playwright-token");
  await expect(mcpCard.locator(".nexusx-mcp-snippet")).toContainText("Bearer mcp_playwright-token");
  await expect(mcpCard.locator(".nexusx-mcp-snippet")).toContainText("mcpServers");
  await page.context().grantPermissions(["clipboard-read", "clipboard-write"], {
    origin: new URL(page.url()).origin,
  });
  await directCard.getByRole("button", { name: "复制查询" }).click();
  await expect(directCard.getByRole("button", { name: "已复制" })).toBeVisible();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toContain("GraphQLCatalogService");
  await mcpCard.getByRole("button", { name: "复制配置" }).click();
  await expect(mcpCard.getByRole("button", { name: "已复制" })).toBeVisible();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toContain("mcpServers");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});

test("a protected route renders the login state after a 401", async ({ page }) => {
  await page.route("**/api/auth/me", async (route) => {
    await route.fulfill({
      status: 401,
      contentType: "application/json",
      body: JSON.stringify({ detail: "authentication required" }),
    });
  });

  await page.goto("/account");
  await expect(page).toHaveURL(/\/login\?redirect=\/account/);
  await expect(page.getByRole("heading", { name: "登录反应路径数据库" })).toBeVisible();
  await expect(page.getByRole("link", { name: "继续登录" })).toBeVisible();
});

test("account settings update the profile, revoke a session, and show audit", async ({ page }) => {
  const user = {
    id: "00000000-0000-7000-8000-000000000971",
    display_name: "Account User",
    primary_email: "account@example.test",
    is_service_account: false,
    identity: { issuer: "https://identity.example.test", subject: "account-user" },
    projects: [],
  };
  const sessions = [
    { id: "00000000-0000-7000-8000-000000000972", created_at: "2026-08-16T00:00:00Z", expires_at: "2026-08-17T00:00:00Z", last_seen_at: "2026-08-16T01:00:00Z", user_agent: "Current Browser", ip_address: "127.0.0.1", current: true },
    { id: "00000000-0000-7000-8000-000000000973", created_at: "2026-08-15T00:00:00Z", expires_at: "2026-08-17T00:00:00Z", last_seen_at: "2026-08-15T01:00:00Z", user_agent: "Other Browser", ip_address: "192.0.2.10", current: false },
  ];

  await page.route("**/api/auth/me", async (route) => {
    if (route.request().method() === "PATCH") {
      user.display_name = (route.request().postDataJSON() as { display_name: string }).display_name;
    }
    await route.fulfill({ contentType: "application/json", body: JSON.stringify(user) });
  });
  await page.route("**/api/organizations", async (route) => {
    await route.fulfill({ contentType: "application/json", body: "[]" });
  });
  await page.route("**/api/auth/sessions**", async (route) => {
    const request = route.request();
    if (request.method() === "GET") {
      await route.fulfill({ contentType: "application/json", body: JSON.stringify(sessions) });
      return;
    }
    if (request.method() === "DELETE") {
      const id = new URL(request.url()).pathname.split("/").at(-1);
      const index = sessions.findIndex((item) => item.id === id);
      if (index >= 0) sessions.splice(index, 1);
    } else {
      sessions.splice(0, sessions.length, ...sessions.filter((item) => item.current));
    }
    await route.fulfill({ status: 204 });
  });
  await page.route("**/api/auth/audit**", async (route) => {
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify([{ id: "00000000-0000-7000-8000-000000000974", created_at: "2026-08-16T01:00:00Z", actor_user_id: user.id, project_id: null, action: "account.profile_updated", entity_type: "user_account", entity_id: user.id, metadata_json: { display_name: "Account User" } }]),
    });
  });

  await page.goto("/account");
  await page.getByLabel("显示名称").fill("Updated Account User");
  await page.getByRole("button", { name: "保存资料" }).click();
  await expect(page.locator(".account-card").getByRole("heading", { name: "Updated Account User" })).toBeVisible();
  await expect(page.getByText("账户资料已更新。", { exact: true })).toBeVisible();

  const otherSession = page.locator(".data-row").filter({ hasText: "Other Browser" });
  await otherSession.getByRole("button", { name: "撤销" }).click();
  await expect(otherSession).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "账户审计" })).toBeVisible();
  await expect(page.getByText("更新账户资料", { exact: true })).toBeVisible();
});

test("a new account can create its first organization and continue to project creation", async ({ page }) => {
  const organizationId = "00000000-0000-7000-8000-000000000991";
  let organizationCreated = false;

  await page.route("**/api/organizations", async (route) => {
    const organization = {
      id: organizationId,
      slug: "new-team",
      name: "New Team",
      status: "active",
      role: "owner",
      can_create_projects: true,
    };
    if (route.request().method() === "POST") {
      organizationCreated = true;
      await route.fulfill({ status: 201, contentType: "application/json", body: JSON.stringify(organization) });
      return;
    }
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(organizationCreated ? [organization] : []),
    });
  });
  await page.route("**/api/projects?*", async (route) => {
    await route.fulfill({ contentType: "application/json", body: "[]" });
  });

  await page.goto("/organizations");
  await expect(page.getByRole("heading", { name: "还没有组织" })).toBeVisible();
  await page.getByRole("button", { name: "新建组织" }).click();
  await page.getByLabel("组织标识").fill("new-team");
  await page.getByLabel("组织名称").fill("New Team");
  await page.getByRole("button", { name: "创建组织", exact: true }).click();

  await expect(page.locator(".organization-card").filter({ hasText: "New Team" })).toBeVisible();
  await page.getByRole("link", { name: /新建项目/ }).click();
  await expect(page.getByRole("heading", { name: "新建项目" })).toBeVisible();
  await expect(page.getByRole("combobox", { name: "组织", exact: true })).toHaveValue(organizationId);
});

test("project management creates, edits, archives, and lists archived projects", async ({ page }) => {
  const organization = {
    id: "00000000-0000-7000-8000-000000000981",
    slug: "managed-team",
    name: "Managed Team",
    status: "active",
    role: "owner",
    can_create_projects: true,
  };
  const projects = [{
    id: "00000000-0000-7000-8000-000000000982",
    organization_id: organization.id,
    organization_slug: organization.slug,
    organization_name: organization.name,
    slug: "initial-project",
    name: "Initial Project",
    status: "active",
    role: "manager",
    organization_role: "owner",
    permissions: ["project:manage"],
    created_at: "2026-08-16T00:00:00Z",
  }];

  await page.route("**/api/organizations", async (route) => {
    await route.fulfill({ contentType: "application/json", body: JSON.stringify([organization]) });
  });
  await page.route("**/api/projects**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname === "/api/projects" && request.method() === "GET") {
      const includeArchived = url.searchParams.get("include_archived") === "true";
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify(projects.filter((project) => includeArchived || project.status !== "archived")),
      });
      return;
    }
    if (url.pathname === "/api/projects" && request.method() === "POST") {
      const body = request.postDataJSON() as { organization_id: string; slug: string; name: string };
      const created = {
        ...projects[0],
        id: "00000000-0000-7000-8000-000000000983",
        organization_id: body.organization_id,
        slug: body.slug,
        name: body.name,
      };
      projects.push(created);
      await route.fulfill({ status: 201, contentType: "application/json", body: JSON.stringify(created) });
      return;
    }
    const project = projects.find((item) => url.pathname.endsWith(item.id));
    expect(project).toBeTruthy();
    Object.assign(project!, request.postDataJSON());
    await route.fulfill({ contentType: "application/json", body: JSON.stringify(project) });
  });

  await page.goto("/projects");
  await expect(page.getByRole("link", { name: "管理组织" })).toHaveCount(0);
  await page.getByRole("button", { name: "新建项目" }).click();
  await page.getByLabel("项目标识").fill("created-project");
  await page.getByLabel("项目名称").fill("Created Project");
  await page.getByRole("button", { name: "创建", exact: true }).click();
  await expect(page.locator(".project-card").filter({ hasText: "Created Project" })).toBeVisible();

  const initial = page.locator(".project-card").first();
  await expect(initial).toContainText("Initial Project");
  await initial.getByRole("button", { name: "编辑" }).click();
  await initial.getByLabel("名称").fill("Renamed Project");
  await initial.getByRole("button", { name: "保存" }).click();
  const renamed = page.locator(".project-card").filter({ hasText: "Renamed Project" });
  await expect(renamed).toBeVisible();

  page.once("dialog", (dialog) => dialog.accept());
  await renamed.getByRole("button", { name: "归档" }).click();
  await expect(renamed).toHaveCount(0);
  await page.getByLabel("显示已归档").check();
  await expect(page.locator(".project-card").filter({ hasText: "Renamed Project" })).toContainText("已归档");
  await page.locator(".project-card").filter({ hasText: "Renamed Project" }).getByRole("button", { name: "恢复" }).click();
  await expect(page.locator(".project-card").filter({ hasText: "Renamed Project" })).toContainText("活跃");
});

test("member roles, removal, invitations, acceptance, and project audit are wired", async ({ page }) => {
  const meResponse = await page.request.get("/api/auth/me");
  const me = (await meResponse.json()) as {
    id: string;
    display_name: string;
    projects: Array<{ project_id: string; permissions: string[] }>;
  };
  const project = me.projects.find((item) => item.permissions.includes("project:manage"));
  expect(project).toBeTruthy();
  const projectId = project!.project_id;
  const secondUserId = "00000000-0000-7000-8000-000000000984";
  const addedUserId = "00000000-0000-7000-8000-000000000987";
  const members = [
    { user_id: me.id, display_name: me.display_name, primary_email: "manager@example.test", role: "manager", created_at: "2026-08-16T00:00:00Z" },
    { user_id: secondUserId, display_name: "Second Member", primary_email: "member@example.test", role: "viewer", created_at: "2026-08-16T00:00:00Z" },
  ];
  const invitations: Array<Record<string, unknown>> = [];

  await page.route(`**/api/projects/${projectId}/members`, async (route) => {
    const request = route.request();
    if (request.method() === "GET") {
      await route.fulfill({ contentType: "application/json", body: JSON.stringify(members) });
      return;
    }
    expect(request.method()).toBe("POST");
    const body = request.postDataJSON() as { user_id: string; role: string };
    const added = { user_id: body.user_id, display_name: "Added Member", primary_email: "added@example.test", role: body.role, created_at: "2026-08-16T00:00:00Z" };
    members.push(added);
    await route.fulfill({ status: 201, contentType: "application/json", body: JSON.stringify(added) });
  });
  await page.route(`**/api/projects/${projectId}/members/*`, async (route) => {
    const request = route.request();
    const userId = new URL(request.url()).pathname.split("/").at(-1);
    const member = members.find((item) => item.user_id === userId);
    expect(member).toBeTruthy();
    if (request.method() === "PATCH") {
      member!.role = (request.postDataJSON() as { role: string }).role;
      await route.fulfill({ contentType: "application/json", body: JSON.stringify(member) });
      return;
    }
    members.splice(members.indexOf(member!), 1);
    await route.fulfill({ status: 204 });
  });
  await page.route(`**/api/projects/${projectId}/invitations`, async (route) => {
    const request = route.request();
    if (request.method() === "GET") {
      await route.fulfill({ contentType: "application/json", body: JSON.stringify(invitations) });
      return;
    }
    const body = request.postDataJSON() as { email: string; role: string };
    const invitation = {
      id: "00000000-0000-7000-8000-000000000985",
      project_id: projectId,
      email: body.email,
      role: body.role,
      created_at: "2026-08-16T00:00:00Z",
      expires_at: "2026-08-23T00:00:00Z",
      accepted_at: null,
      revoked_at: null,
      delivery_status: "link_only",
      delivery_error: null,
    };
    invitations.splice(0, invitations.length, invitation);
    await route.fulfill({
      status: 201,
      contentType: "application/json",
      body: JSON.stringify({ invitation, accept_token: "invite-token", accept_url: "http://127.0.0.1:5176/invitations/invite-token", delivery_status: "link_only", delivery_error: null }),
    });
  });
  await page.route("**/api/users?*", async (route) => {
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({ items: [{ id: addedUserId, display_name: "Added Member", primary_email: "added@example.test", status: "active", is_service_account: false, last_authenticated_at: "2026-08-16T00:00:00Z", created_at: "2026-08-16T00:00:00Z", project_role: null }], total: 1, limit: 50, offset: 0 }),
    });
  });
  await page.route(`**/api/projects/${projectId}/audit**`, async (route) => {
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify([{ id: "00000000-0000-7000-8000-000000000986", created_at: "2026-08-16T00:00:00Z", actor_user_id: me.id, project_id: projectId, action: "project.member.role_changed", entity_type: "project_membership", entity_id: secondUserId, metadata_json: { role: "contributor" } }]),
    });
  });
  await page.route("**/api/auth/invitations/invite-token/accept", async (route) => {
    await route.fulfill({ contentType: "application/json", body: JSON.stringify(invitations[0]) });
  });

  await page.goto(`/projects/${projectId}/members?project_id=${projectId}`);
  await expect(page.getByRole("heading", { name: "项目成员" })).toBeVisible();
  await page.getByLabel("Second Member 的角色").selectOption("contributor");
  await expect.poll(() => members.find((item) => item.user_id === secondUserId)?.role).toBe("contributor");
  page.once("dialog", (dialog) => dialog.accept());
  await page.locator(".data-row").filter({ hasText: "Second Member" }).getByRole("button", { name: "移除" }).click();
  await expect(page.locator(".data-row").filter({ hasText: "Second Member" })).toHaveCount(0);

  await page.getByRole("button", { name: "添加已有用户" }).click();
  await page.getByLabel("添加成员用户").selectOption(addedUserId);
  await page.getByRole("button", { name: "添加成员", exact: true }).click();
  await expect(page.locator(".data-row").filter({ hasText: "Added Member" })).toBeVisible();

  await page.getByLabel("邮箱").fill("invitee@example.test");
  await page.getByRole("button", { name: "生成邀请链接" }).click();
  await expect(page.locator(".invite-result")).toContainText("/invitations/invite-token");
  await expect(page.getByRole("heading", { name: "项目审计" })).toBeVisible();
  await expect(page.getByText("修改成员角色", { exact: true })).toBeVisible();

  await page.goto("/invitations/invite-token");
  await page.getByRole("button", { name: "接受邀请" }).click();
  await expect(page).toHaveURL(/\/projects/);
});

test("a recovered session clears the outage notice and returns to the original route", async ({ page }) => {
  let unavailable = true;

  await page.route("**/api/auth/me", async (route) => {
    if (unavailable) {
      await route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({ detail: "controlled session outage" }),
      });
      return;
    }
    await route.fallback();
  });

  await page.goto("/geometries?search=water");
  await expect(page).toHaveURL(/\/artifacts\?.*unavailable=true/);
  await expect(page.getByText("服务暂时不可用；当前仍可浏览公开 Artifact。", { exact: true })).toBeVisible();

  unavailable = false;
  await expect(page).toHaveURL(/\/geometries\?/, { timeout: 10_000 });
  await expect(page).toHaveURL(/search=water/);
  await expect(page).not.toHaveURL(/unavailable|redirect=/);
  await expect(page.getByText("服务暂时不可用；当前仍可浏览公开 Artifact。", { exact: true })).toHaveCount(0);
});

test("upload queue isolates failures, retries them, and can cancel an active request", async ({ page }) => {
  let batchSequence = 0;
  let currentBatchId = "";
  let retryableAttempts = 0;
  let cancelledUploadStartedResolve!: () => void;
  const cancelledUploadStarted = new Promise<void>((resolve) => {
    cancelledUploadStartedResolve = resolve;
  });
  let releaseCancelledUpload: (() => void) | null = null;
  const manifests = new Map<string, Map<string, { name: string; size: number }>>();
  const statuses = new Map<string, "queued" | "succeeded" | "failed" | "cancelled">();

  await page.route("**/api/upload-batches**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname === "/api/upload-batches" && request.method() === "GET") {
      await route.fulfill({ contentType: "application/json", body: JSON.stringify({ items: [], total: 0, limit: 8, offset: 0 }) });
      return;
    }
    if (url.pathname === "/api/upload-batches" && request.method() === "POST") {
      batchSequence += 1;
      currentBatchId = `00000000-0000-7000-8000-${String(810 + batchSequence).padStart(12, "0")}`;
      const body = request.postDataJSON() as { files: Array<{ client_file_id: string; original_filename: string; size_bytes: number }> };
      const manifest = new Map(body.files.map((file) => [file.client_file_id, { name: file.original_filename, size: file.size_bytes }]));
      manifests.set(currentBatchId, manifest);
      for (const clientFileId of manifest.keys()) statuses.set(clientFileId, "queued");
      await route.fulfill({ status: 201, contentType: "application/json", body: JSON.stringify(uploadBatchFixture(currentBatchId, { total_count: manifest.size, total_bytes: [...manifest.values()].reduce((sum, file) => sum + file.size, 0), artifact_kind: "calculation_output", shared_metadata: {} })) });
      return;
    }
    const batchMatch = url.pathname.match(/^\/api\/upload-batches\/([^/]+)$/);
    if (batchMatch && request.method() === "GET") {
      const manifest = manifests.get(batchMatch[1]) ?? new Map();
      const values = [...manifest.keys()].map((id) => statuses.get(id));
      const succeeded = values.filter((value) => value === "succeeded").length;
      const failed = values.filter((value) => value === "failed").length;
      const cancelled = values.filter((value) => value === "cancelled").length;
      await route.fulfill({ contentType: "application/json", body: JSON.stringify(uploadBatchFixture(batchMatch[1], { total_count: manifest.size, total_bytes: [...manifest.values()].reduce((sum, file) => sum + file.size, 0), succeeded_count: succeeded, failed_count: failed, cancelled_count: cancelled, status: succeeded + failed + cancelled === manifest.size ? "completed" : "active" })) });
      return;
    }
    if (batchMatch && request.method() === "DELETE") {
      const manifest = manifests.get(batchMatch[1]) ?? new Map();
      for (const clientFileId of manifest.keys()) {
        if (statuses.get(clientFileId) === "queued") statuses.set(clientFileId, "cancelled");
      }
      await route.fulfill({ contentType: "application/json", body: JSON.stringify(uploadBatchFixture(batchMatch[1], { status: "cancelled", total_count: manifest.size, cancelled_count: manifest.size })) });
      return;
    }
    const retryMatch = url.pathname.match(/\/items\/([^/]+)\/retry$/);
    if (retryMatch && request.method() === "POST") {
      const clientFileId = retryMatch[1];
      const file = manifests.get(currentBatchId)?.get(clientFileId);
      statuses.set(clientFileId, "queued");
      await route.fulfill({ contentType: "application/json", body: JSON.stringify(uploadBatchItemFixture(clientFileId, file?.name ?? "retry.log", { size_bytes: file?.size ?? 0, status: "queued", artifact_file_id: null, attempt_count: 1 })) });
      return;
    }
    if (!url.pathname.endsWith(`/upload-batches/${currentBatchId}/files`) || request.method() !== "POST") {
      await route.fallback();
      return;
    }
    const manifest = manifests.get(currentBatchId) ?? new Map();
    const cancelFile = [...manifest.entries()].find(([, file]) => file.name === "cancel.log");
    if (cancelFile) {
      cancelledUploadStartedResolve();
      await new Promise<void>((release) => { releaseCancelledUpload = release; });
    }
    const items = [...manifest.entries()].map(([clientFileId, file]) => {
      if (file.name === "retry.log" && retryableAttempts++ === 0) {
        statuses.set(clientFileId, "failed");
        return uploadBatchItemFixture(clientFileId, file.name, {
          size_bytes: file.size,
          status: "failed",
          artifact_file_id: null,
          error_code: "ingestion_failed",
          error_message: "controlled upload failure",
        });
      }
      statuses.set(clientFileId, "succeeded");
      return uploadBatchItemFixture(clientFileId, file.name, { size_bytes: file.size });
    });
    await route.fulfill({ contentType: "application/json", body: JSON.stringify(items) });
  });

  await page.goto("/uploads");
  const input = page.locator('.upload-source-actions input[type="file"]').first();
  await input.setInputFiles([
    { name: "success.log", mimeType: "text/plain", buffer: Buffer.from("success") },
    { name: "retry.log", mimeType: "text/plain", buffer: Buffer.from("retry") },
  ]);
  await page.getByRole("button", { name: "开始上传" }).click();

  const retryRow = page.locator(".upload-task-row").filter({ hasText: "retry.log" });
  await expect(retryRow).toContainText("失败");
  await retryRow.getByRole("button", { name: "重试文件" }).click();
  await expect(retryRow).toHaveCount(0);
  expect(retryableAttempts).toBe(2);

  await expect(page.getByRole("heading", { name: "批量文件上传" })).toBeVisible();
  await expect(page.getByRole("button", { name: "新建批次" })).toHaveCount(0);
  await expect(page.locator(".upload-source header")).toContainText("0 个");
  await input.setInputFiles({ name: "cancel.log", mimeType: "text/plain", buffer: Buffer.from("cancel") });
  await page.getByRole("button", { name: "开始上传" }).click();
  await cancelledUploadStarted;
  await page.getByRole("button", { name: "取消批次" }).click();
  await expect(page.locator(".upload-task-row").filter({ hasText: "cancel.log" })).toContainText("取消");
  releaseCancelledUpload?.();
});

test("project manager can confirm and delete an artifact", async ({ page }) => {
  const projectId = "00000000-0000-7000-8000-000000000920";
  const artifact = {
    id: "00000000-0000-7000-8000-000000000921",
    original_filename: "remove-from-catalog.log",
    size_bytes: 18,
    content_sha256: "b".repeat(64),
    visibility: "project",
    artifact_kind: "calculation_output",
    storage_status: "available",
    project_id: projectId,
    created_by_user_id: "00000000-0000-7000-8000-000000000922",
    media_type: "text/plain",
    storage_verified_at: "2026-08-14T00:00:00Z",
    preview_available: true,
  };
  let deleted = false;

  await page.route("**/api/auth/me", async (route) => {
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        id: artifact.created_by_user_id,
        display_name: "Artifact Manager",
        primary_email: "manager@example.test",
        is_service_account: false,
        identity: { issuer: "https://issuer.example.test", subject: "artifact-manager" },
        projects: [{
          project_id: projectId,
          project_slug: "managed-project",
          project_name: "Managed Project",
          organization_id: "00000000-0000-7000-8000-000000000923",
          organization_slug: "test-organization",
          organization_name: "Test Organization",
          organization_role: "member",
          project_role: "manager",
          permissions: ["artifact:read", "artifact:download", "artifact:delete"],
        }],
      }),
    });
  });
  await page.route("**/api/artifacts?*", async (route) => {
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        items: deleted ? [] : [artifact],
        page: { total: deleted ? 0 : 1, limit: 50, offset: 0 },
      }),
    });
  });
  await page.route(`**/api/artifacts/${artifact.id}`, async (route) => {
    expect(route.request().method()).toBe("DELETE");
    deleted = true;
    await route.fulfill({ status: 204 });
  });

  await page.goto("/artifacts");
  const artifactRow = page.locator("tbody tr").filter({ hasText: artifact.original_filename });
  await expect(artifactRow).toBeVisible();
  page.once("dialog", async (dialog) => {
    expect(dialog.message()).toContain(artifact.original_filename);
    expect(dialog.message()).toContain("无法撤销");
    await dialog.accept();
  });
  await artifactRow.getByRole("button", { name: `删除文件 ${artifact.original_filename}` }).click();

  await expect(artifactRow).toHaveCount(0);
  await expect(page.getByRole("status")).toContainText(`已删除：${artifact.original_filename}`);
  expect(deleted).toBe(true);
});

test("anonymous user can preview a public artifact and access its download link", async ({ page }, testInfo) => {
  const artifact = {
    id: "00000000-0000-7000-8000-000000000901",
    original_filename: "public-gaussian.log",
    size_bytes: 26,
    content_sha256: "693fba26e4552dac665428bf9d65810ad261b5885a0519ad64ce8572922fe462",
    visibility: "public",
    artifact_kind: "calculation_output",
    storage_status: "available",
    project_id: "00000000-0000-7000-8000-000000000902",
    created_by_user_id: "00000000-0000-7000-8000-000000000903",
    media_type: "text/plain",
    storage_verified_at: "2026-08-14T00:00:00Z",
    preview_available: true,
  };
  const publicPayload = "Gaussian public test data\n";

  await page.route("**/api/auth/me", async (route) => {
    await route.fulfill({
      status: 401,
      contentType: "application/json",
      body: JSON.stringify({ detail: "authentication required" }),
    });
  });
  await page.route("**/api/artifacts?*", async (route) => {
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({ items: [artifact], page: { total: 1, limit: 50, offset: 0 } }),
    });
  });
  await page.route(`**/api/artifacts/${artifact.id}/preview?*`, async (route) => {
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        artifact_id: artifact.id,
        id: artifact.id,
        original_filename: artifact.original_filename,
        media_type: artifact.media_type,
        size_bytes: artifact.size_bytes,
        content_sha256: artifact.content_sha256,
        preview_text: publicPayload,
        preview_bytes: artifact.size_bytes,
        truncated: false,
      }),
    });
  });

  await page.goto("/");
  await expect(page.getByText("匿名访问", { exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "原始文件" })).toBeVisible();

  const artifactTableLayout = await page.locator(".data-table-wrap").evaluate((wrapper) => {
    const table = wrapper.querySelector<HTMLTableElement>("table.artifacts-table");
    const header = table?.tHead;
    if (!table || !header) throw new Error("artifact table is incomplete");
    return {
      display: getComputedStyle(table).display,
      wrapperWidth: wrapper.clientWidth,
      tableWidth: table.getBoundingClientRect().width,
      headerWidth: header.getBoundingClientRect().width,
    };
  });
  expect(artifactTableLayout.display).toBe("table");
  expect(artifactTableLayout.tableWidth + 1).toBeGreaterThanOrEqual(artifactTableLayout.wrapperWidth);
  expect(Math.abs(artifactTableLayout.headerWidth - artifactTableLayout.tableWidth)).toBeLessThanOrEqual(1);

  const artifactRow = page.locator("tbody tr").filter({ hasText: artifact.original_filename });
  await expect(artifactRow.getByRole("button", { name: /删除文件/ })).toHaveCount(0);
  await artifactRow.getByRole("button", { name: "预览文件" }).click();
  await expect(page.getByRole("heading", { name: "文件预览" })).toBeVisible();
  await expect(page.locator(".artifact-preview-text")).toContainText("Gaussian");
  await page.waitForTimeout(250);
  await page.screenshot({ path: testInfo.outputPath("anonymous-artifact-preview.png") });

  const downloadLink = page.getByRole("link", { name: "下载原文件" });
  await expect(downloadLink).toHaveAttribute("href", `/api/artifacts/${artifact.id}/download`);
  await expect(downloadLink).toHaveAttribute("download", artifact.original_filename);
});
