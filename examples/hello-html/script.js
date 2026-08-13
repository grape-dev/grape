const btn = document.getElementById('clickBtn');
const output = document.getElementById('output');

let count = 0;
btn.addEventListener('click', () => {
  count += 1;
  output.textContent = `你点击了 ${count} 次！`;
});
