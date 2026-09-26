const paymentModal = document.querySelector('#payment-modal');
const paymentFields = {
  plan: document.querySelector('#payment-plan'),
  bank: document.querySelector('#payment-bank'),
  accountName: document.querySelector('#payment-account-name'),
  accountNumber: document.querySelector('#payment-account-number'),
  reference: document.querySelector('#payment-reference'),
  instructions: document.querySelector('#payment-instructions'),
  id: document.querySelector('#payment-id'),
  referenceInput: document.querySelector('#reference'),
};

function closePaymentModal() {
  if (paymentModal) paymentModal.hidden = true;
}

document.querySelectorAll('.pay-now').forEach((button) => {
  button.addEventListener('click', async () => {
    button.disabled = true;
    try {
      const response = await fetch('/billing/payment-intent', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ plan: button.dataset.plan }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'Could not start payment.');
      paymentFields.plan.textContent = `${data.plan} - ${data.currency}${data.amount}`;
      paymentFields.bank.textContent = data.bank_name || 'Not configured yet';
      paymentFields.accountName.textContent = data.account_name || 'Not configured yet';
      paymentFields.accountNumber.textContent = data.account_number || 'Not configured yet';
      paymentFields.reference.textContent = data.reference_code;
      paymentFields.instructions.textContent = data.instructions;
      paymentFields.id.value = data.payment_id;
      paymentFields.referenceInput.value = data.reference_code;
      paymentModal.hidden = false;
    } catch (error) {
      window.alert(error.message);
    } finally {
      button.disabled = false;
    }
  });
});

document.querySelectorAll('[data-close-payment]').forEach((element) => element.addEventListener('click', closePaymentModal));
document.addEventListener('keydown', (event) => { if (event.key === 'Escape') closePaymentModal(); });

document.querySelectorAll('.copy-detail').forEach((button) => {
  button.addEventListener('click', async () => {
    const value = document.querySelector(`#${button.dataset.copyTarget}`).textContent;
    await navigator.clipboard.writeText(value);
    button.innerHTML = '<i class="bi bi-check2"></i>';
    window.setTimeout(() => { button.innerHTML = '<i class="bi bi-copy"></i>'; }, 1300);
  });
});
