class Stage < Formula
  include Language::Python::Virtualenv

  desc "Aggregates CS internship postings (Canada/US, EN + FR) into a local SQLite database"
  homepage "https://github.com/NicholasXydis/Stage"
  url "https://files.pythonhosted.org/packages/46/d7/934844ae10e2337af8543b3b91d3c9c0b6b09bdddcf7bb38405fcbe778c4/stage_cli-1.0.0.tar.gz"
  sha256 "962a439b690733ea467872dc343dfc1d8cf24a53ccef6ba50390e3b63a1a1cd1"
  license "MIT"

  depends_on "python@3.12"

  def install
    virtualenv_install_with_resources
  end

  test do
    assert_match version.to_s, shell_output("#{bin}/stage --version")
    assert_match "Usage:", shell_output("#{bin}/stage --help")
  end
end
