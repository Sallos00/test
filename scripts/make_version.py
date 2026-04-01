# -*- coding: utf-8 -*-
content = """VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=(3, 0, 0, 0),
    prodvers=(3, 0, 0, 0),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        u'041204B0',
        [
          StringStruct(u'CompanyName',      u''),
          StringStruct(u'FileDescription',  u'Auto Sinc'),
          StringStruct(u'FileVersion',      u'3.0.0.0'),
          StringStruct(u'InternalName',     u'AutoSinc'),
          StringStruct(u'LegalCopyright',   u'Sinamon'),
          StringStruct(u'OriginalFilename', u'Auto Sinc.exe'),
          StringStruct(u'ProductName',      u'Auto Sinc'),
          StringStruct(u'ProductVersion',   u'3.0.0.0'),
        ]
      )
    ]),
    VarFileInfo([VarStruct(u'Translation', [0x0412, 0x04B0])])
  ]
)"""

with open("version_info.txt", "w", encoding="utf-8") as f:
    f.write(content.strip())
print("version_info.txt created")
