components {
  id: "script"
  component: "/main/item.script"
}
components {
  id: "sprite"
  component: "/main/item.sprite"
  scale {
    x: 2.0
    y: 2.0
  }
}
embedded_components {
  id: "label"
  type: "label"
  data: "size {\n"
  "  x: 120.0\n"
  "  y: 32.0\n"
  "}\n"
  "shadow {\n"
  "  w: 0.45\n"
  "}\n"
  "text: \"L1\"\n"
  "font: \"/builtins/fonts/default.font\"\n"
  "material: \"/builtins/fonts/label-df.material\"\n"
  ""
  position {
    z: 0.2
  }
}
