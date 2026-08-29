# QM parser fixtures

[English](README.md) | [简体中文](README.zh-CN.md)

`minimal_orca_water_sp.orcaout` 是从用于导入测试的真实 ORCA 输出字段结构中提取的精简
ORCA 6.1.1 single-point output。它只保留确定性 water calculation 所需的 parser contract
evidence：banner/version、打印输入、Angstrom 坐标、SCF 收敛、最终能量、正常终止与 runtime。

该 fixture 有意独立于文件名和目录布局。测试将它作为 `unstructured-upload.bin` 交给 MolOP，
并要求 content probe 将其识别为 `orcaout`。
