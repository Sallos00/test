# -*- coding: utf-8 -*-
content = """VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=(2, 0, 0, 0),
    prodvers=(2, 0, 0, 0),
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
          StringStruct(u'FileDescription',  u'PotPlayer \uc74c\uc131 \uc2f1\ud06c \uc790\ub3d9 \ubcf4\uc815\uae30'),
          StringStruct(u'FileVersion',      u'2.0.0.0'),
          StringStruct(u'InternalName',     u'PotPlayerLipsync'),
          StringStruct(u'LegalCopyright',   u'Sinamon'),
          StringStruct(u'OriginalFilename', u'Auto Sync.exe'),
          StringStruct(u'ProductName',      u'Auto Sync'),
          StringStruct(u'ProductVersion',   u'2.0.0.0'),
        ]
      )
    ]),
    VarFileInfo([VarStruct(u'Translation', [0x0412, 0x04B0])])
  ]
)"""

with open("version_info.txt", "w", encoding="utf-8") as f:
    f.write(content.strip())
print("version_info.txt created")
