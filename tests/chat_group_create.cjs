async function openGroupCreation(page, title) {
  await page.locator("#chatGroupNew").click();
  const dialog = page.locator("#chatGroupCreateDialog");
  await dialog.waitFor({ state: "visible" });
  const name = dialog.locator("#chatGroupCreateName");
  await name.fill(title);
  return { dialog, name, submit: dialog.locator("#chatGroupCreateSubmit") };
}

async function submitGroupCreation(page, title) {
  const { submit } = await openGroupCreation(page, title);
  await submit.click();
}

module.exports = { openGroupCreation, submitGroupCreation };
