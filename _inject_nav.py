with open(r'F:\ai workspace\pdd-inventory\gui.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

for i, line in enumerate(lines):
    if '_sort_reverse = False' in line:
        insert_idx = i + 2
        break

nav_code = '''
    def _toggle_nav(self):
        if self.nav_frame.winfo_ismapped():
            self.nav_frame.pack_forget()
        else:
            self.nav_frame.pack(side="left", fill="y")
            if not self.nav_buttons:
                self._build_nav()

    def _build_nav(self):
        items = [
            ("HOME", self.page_home),
            ("CONFIG", self.page_general),
            ("PRODUCT", self.page_products),
            ("THEME", self.page_theme),
            ("BACKEND", self.page_backend),
            ("CALIB", self.page_calibrate),
        ]
        for text, page in items:
            btn = tk.Button(self.nav_frame, text=text, relief="flat",
                           font=(self.FONT[0], 9), anchor="w", padx=12, pady=6,
                           command=lambda p=page: self._show_page(p))
            btn.pack(fill="x")
            self.nav_buttons[text] = btn

    def _show_page(self, page):
        if self._current_page:
            self._current_page.pack_forget()
        page.pack(fill="both", expand=True)
        self._current_page = page
        if page == self.page_general and not hasattr(page, '_built'):
            self._build_general_page()
        elif page == self.page_products and not hasattr(page, '_built'):
            self._build_product_region_tab(page)
        elif page == self.page_theme and not hasattr(page, '_built'):
            self._build_skin_tab(page)
        elif page == self.page_backend and not hasattr(page, '_built'):
            self._build_backend_tab(page)
        elif page == self.page_calibrate and not hasattr(page, '_built'):
            self._build_calibrate_tab(page)
        self._apply_theme(self._theme_name)

    '''

new_lines = nav_code.split('\n')
for j, nl in enumerate(new_lines):
    lines.insert(insert_idx + j, nl + '\n')

with open(r'F:\ai workspace\pdd-inventory\gui.py', 'w', encoding='utf-8') as f:
    f.writelines(lines)

import py_compile
py_compile.compile(r'F:\ai workspace\pdd-inventory\gui.py', doraise=True)
print('OK')