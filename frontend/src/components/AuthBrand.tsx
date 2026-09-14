import { STATIC_ASSETS } from '../config/staticAssets';

export function AuthBrand() {
  return (
    <div className="auth-brand-logo">
      <img src={STATIC_ASSETS.companyLogo} alt="Bosch" />
    </div>
  );
}
