using System.Windows;
using GChartWpf.ViewModels;

namespace GChartWpf;

public partial class MainWindow : Window
{
    readonly MainViewModel _vm;

    public MainWindow()
    {
        InitializeComponent();
        _vm = new MainViewModel(AppSettings.CreateSource());
        DataContext = _vm;
        Closed += (_, _) => _vm.Stop();
    }
}
