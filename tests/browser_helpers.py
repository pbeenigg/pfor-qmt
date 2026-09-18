from playwright.sync_api import expect


def choose_many(page, selector, *values):
    field=page.locator(selector)
    identifier=field.get_attribute('id')
    page.locator(f'[data-multi-for="{identifier}"]').click()
    page.locator('#option-clear').click()
    for value in values:
        option=page.locator(f'#option-rows input[value="{value}"]')
        expect(option).to_be_visible()
        option.check()
    page.locator('#option-apply').click()
    expect(page.locator('#option-picker')).not_to_be_visible()
