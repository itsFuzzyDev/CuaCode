Tools dir, builds all tools automatically, attches into provider. _parser folder handles turning each tool schmea into required schema for the AI providers

tools:
- [x] screenshot (grid_size)
- [x] mouse_move (x, y) 
- [x] click (x, y, button)
- [x] type_text (text)
- [x] key (key combo, e.g. "cmd+c")
- [x] scroll (x, y, dx, dy)
- [x] wait (seconds)
- [x] app_open (app_name)
- [x] app_list - includes closed and opened apps.