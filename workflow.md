# git工作流
## 拉取仓库
```bash
 git clone git@github.com:isxiaohe/pkudsa.avalon.git
 git checkout -b <你的名字>
```
## 提交修改
```bash
 git add <你修改的文件>
 git commit -m "给修改命名吧"
 git push origin <你的名字>
```
## 更新代码
```bash
 git checkout main
 git pull origin main
 git checkout <你的名字>
 git rebase main
```