from playwright.sync_api import expect


def choose_many(page, selector, *values):
    if selector=='#futures-form [name=resource]' and len(values)==1:
        navigate(page,values[0])
        return
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


def navigate(page, route):
    route={'market':'catalog','futures':'calendar','operations':'overview','settings':'accounts'}.get(route,route)
    if page.locator('#nav-toggle').is_visible() and not page.locator('body').evaluate("el=>el.classList.contains('nav-open')"):
        page.locator('#nav-toggle').click()
    page.locator(f'nav [data-route="{route}"]').click()
    expect(page).to_have_url(__import__('re').compile('#'+route+'$'))


def sync_report(page):
    page.locator('#futures-sync').click()
    page.get_by_role('button',name='预览并采集',exact=True).click()
    page.locator('#batch-submit').click()


def confirm_export(page, selector):
    page.locator(selector).click()
    page.locator('#batch-submit').click()


def new_dataset(page):
    navigate(page,'datasets')
    page.get_by_role('button',name='新建数据集',exact=True).click()
