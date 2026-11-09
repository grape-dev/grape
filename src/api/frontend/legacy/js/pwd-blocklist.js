// 自动生成：与后端 src/auth/password.py _WEAK_PASSWORDS 同源（后端更新后须重新生成，scripts/gen-pwd-blocklist.py）
// 勿手改
window.PWD_BLOCKLIST = new Set(["00000000", "11111111", "11112222", "12341234", "12344321", "12345678", "123456789", "1234567890", "12345678901", "1q2w3e4r", "1q2w3e4r5t", "22222222", "33333333", "44444444", "52013141", "52113141", "55555555", "66666666", "77777777", "88888888", "99999999", "a123456789", "aa123456789", "ab123456789", "abc12345", "abcd1234", "abcdef12", "admin123", "admin1234", "administrator", "asd12345", "asdasd123", "changeme", "changeme1", "dragon1", "dragon123", "football1", "football123", "guest1", "guest123", "iloveyou", "iloveyou1", "jennifer1", "jennifer123", "letmein1", "letmein123", "li123456", "login1", "login123", "master1", "master123", "michael1", "michael123", "monkey1", "monkey123", "p@ssw0rd", "p@ssword", "passw0rd", "passw0rd1", "password", "password1", "password12", "password123", "princess1", "princess123", "qaz12345", "qazwsx123", "qq123456", "qq1234567", "qq12345678", "qwerty1", "qwerty123", "qwertyuiop", "root1234", "root12345", "shadow1", "shadow123", "starwars1", "starwars123", "sunshine1", "sunshine123", "superman1", "superman123", "test1234", "test12345", "toor1234", "trustno1", "trustno12", "wang1234", "wang12345", "welcome1", "welcome123", "woaini131", "woaini520", "woaini521", "z123456789", "zhang1234", "zxcvbnm1", "zxcvbnm12"]);
// 唯一谓词（复核 #3 收敛，2026-10-28）：所有前端密码入口（注册/改密/设密/忘记密码）
// 统一消费本函数，不得在各调用点散落 PWD_BLOCKLIST.has 拷贝——判断逻辑单点定义，
// 数据单源（本文件），消费面变更只需改调用点、名单变更只需重跑生成器。
window.isWeakPassword = function (p) {
  return !!(p && window.PWD_BLOCKLIST && window.PWD_BLOCKLIST.has(String(p).trim().toLowerCase()));
};
// 最短长度常量（C 家族收敛，2026-10-28）：与后端 check_password_strength 的下限同源，
// 前端散落的 length<8 / length>=8 硬编码统一消费本常量——后端调下限时重跑生成器即全端同步。
window.PWD_MIN_LEN = 8;
window.isPwdTooShort = function (p) {
  return !p || String(p).length < window.PWD_MIN_LEN;
};
